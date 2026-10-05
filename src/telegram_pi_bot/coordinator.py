"""Pure conversation state transitions for the Telegram/Pi boundary."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from telegram_pi_bot.model import (
    BlockingUiState,
    BotState,
    BundleItem,
    ConversationAction,
    Effect,
    InputBundle,
    ModelRef,
    PendingSession,
    SessionConfig,
    SessionRef,
    StoredArtifact,
    Transition,
    TurnRecord,
)


TEXT_DELAY_MS = 5_000
MEDIA_DELAY_MS = 10_000
HOLD_DELAY_MS = 2 * 60 * 1_000
UI_TIMEOUT_MS = 10 * 60 * 1_000
PENDING_RETENTION_MS = 24 * 60 * 60 * 1_000
TERMINAL_TURN_STATUSES = {
    "completed",
    "handled",
    "aborted",
    "failed",
    "uncertain",
    "rejected",
}
PROCESS_EFFECT_KINDS = {
    "dispatch_turn",
    "run_session_operation",
    "steer_turn",
    "abort_turn",
    "answer_ui",
    "cancel_ui",
}
INPUT_ACTION_KINDS = {"add_text", "add_voice", "add_photo", "add_document"}
REJECTED_PROMPT_REPLY = (
    "Pi rejected the prompt before accepting it. Use Send to retry or Cancel "
    "to discard it."
)


def transition(state: BotState, action: ConversationAction) -> Transition:
    """Return one deterministic next state and its ordered side effects."""

    handlers = {
        "add_text": _add_input,
        "add_voice": _add_input,
        "add_photo": _add_input,
        "add_document": _add_input,
        "bundle_timer_fired": _dispatch_bundle,
        "send_now": _dispatch_bundle,
        "cancel_bundle": _cancel_bundle,
        "hold_bundle": _hold_bundle,
        "steer_current": _steer_current,
        "stop": _stop,
        "new_session": _new_session,
        "use_session": _use_session,
        "configure_session": _configure_session,
        "begin_session_operation": _begin_session_operation,
        "session_operation_completed": _session_operation_completed,
        "materialized_session": _materialize_session,
        "recover_materialization": _recover_materialization,
        "turn_accepted": _turn_accepted,
        "turn_completed": _turn_completed,
        "delivery_completed": _delivery_completed,
        "blocking_ui_opened": _blocking_ui_opened,
        "answer_ui": _answer_ui,
        "blocking_ui_timeout": _blocking_ui_timeout,
        "bundle_dispatched": _bundle_dispatched,
        "steer_dispatched": _steer_dispatched,
        "artifact_queued": _artifact_queued,
        "recover": _recover,
        "prune": _prune,
    }
    handler = handlers.get(action.kind)
    if handler is None:
        return Transition(state, action_id=_action_id(state, action))
    result = handler(state, action)
    caused_by = action.get("caused_by_effect_id")
    if (
        isinstance(caused_by, str)
        and caused_by
        and result.state.version != state.version
    ):
        settled = dict(result.settled_effects)
        settled[caused_by] = "done"
        result = replace(result, settled_effects=settled)
    return result


def _add_input(state: BotState, action: ConversationAction) -> Transition:
    now_ms = _now(state, action)
    item_kind = action.kind.removeprefix("add_")
    value = action.text if item_kind == "text" else action.get("attachment_id")
    source_message_id = action.get("source_message_id")
    if not isinstance(value, str) or not value or not _positive_int(source_message_id):
        return Transition(state, action_id=_action_id(state, action))
    if any(
        item.source_message_id == source_message_id
        for bundle in state.bundles.values()
        for item in bundle.items
    ) or any(
        turn.source_message_id == source_message_id
        for turn in state.turns.values()
    ):
        return Transition(state, action_id=_action_id(state, action))
    item = BundleItem(item_kind, value, source_message_id)
    media = item_kind != "text"
    slot = "next_bundle" if _has_active_lease(state) else "bundle"
    current = getattr(state, slot)
    moved_frozen: InputBundle | None = None
    if slot == "bundle" and current is not None and current.status == "frozen":
        if state.next_bundle is None:
            moved_frozen = current
            current = None
        else:
            slot = "next_bundle"
            current = state.next_bundle
    effects: list[Effect] = []
    if current is not None and current.status == "frozen":
        bundle = replace(
            current,
            kind=("media" if media or current.kind == "media" else "text"),
            items=(*current.items, item),
            expires_at_ms=now_ms + PENDING_RETENTION_MS,
        )
    elif current is None or current.status not in {"open", "queued"}:
        bundle_id = action.get("bundle_id") or (
            f"bundle-{state.chat_id}-{source_message_id}"
        )
        bundle = InputBundle(
            bundle_id=bundle_id,
            kind="media" if media else "text",
            status="queued" if slot == "next_bundle" else "open",
            items=(item,),
            due_at_ms=now_ms + (MEDIA_DELAY_MS if media else TEXT_DELAY_MS),
            timer_generation=1,
            created_at_ms=now_ms,
            expires_at_ms=now_ms + PENDING_RETENTION_MS,
        )
    else:
        is_media = media or current.kind == "media"
        delay_ms = (
            HOLD_DELAY_MS
            if current.held
            else MEDIA_DELAY_MS
            if is_media
            else TEXT_DELAY_MS
        )
        bundle = replace(
            current,
            kind="media" if is_media else "text",
            items=(*current.items, item),
            due_at_ms=now_ms + delay_ms,
            timer_generation=current.timer_generation + 1,
            expires_at_ms=now_ms + PENDING_RETENTION_MS,
        )
        effects.append(
            _effect(
                state,
                action,
                len(effects),
                "cancel_timer",
                bundle_id=bundle.bundle_id,
            )
        )
    if bundle.status != "frozen":
        effects.append(
            _effect(
                state,
                action,
                len(effects),
                "schedule_timer",
                bundle_id=bundle.bundle_id,
                generation=bundle.timer_generation,
                due_at_ms=bundle.due_at_ms,
            )
        )
    effects.append(
        _effect(
            state,
            action,
            len(effects),
            "react",
            source_message_id=source_message_id,
            emoji="👀",
        )
    )
    bundles = dict(state.bundles)
    bundles[bundle.bundle_id] = bundle
    changes: dict[str, Any] = {slot: bundle, "bundles": bundles}
    if moved_frozen is not None:
        changes["next_bundle"] = moved_frozen
    next_state = _advance(state, now_ms, **changes)
    return Transition(next_state, tuple(effects), _action_id(state, action))


def _dispatch_bundle(state: BotState, action: ConversationAction) -> Transition:
    bundle_id = action.get("bundle_id")
    if not isinstance(bundle_id, str):
        return Transition(state, action_id=_action_id(state, action))
    bundle = _live_bundle(state, bundle_id)
    if bundle is None or bundle.status not in {"open", "queued", "frozen"}:
        return Transition(state, action_id=_action_id(state, action))
    now_ms = _now(state, action)
    if action.kind == "bundle_timer_fired":
        if action.get("generation") != bundle.timer_generation:
            return Transition(state, action_id=_action_id(state, action))
        if bundle.due_at_ms is None or now_ms < bundle.due_at_ms:
            return Transition(state, action_id=_action_id(state, action))
    if _has_active_lease(state):
        if bundle.status == "frozen":
            return Transition(state, action_id=_action_id(state, action))
        queued = replace(bundle, status="queued", due_at_ms=None)
        bundles = dict(state.bundles)
        bundles[bundle_id] = queued
        next_state = _advance(
            state,
            now_ms,
            bundle=(
                None
                if state.bundle is not None
                and state.bundle.bundle_id == bundle_id
                else state.bundle
            ),
            next_bundle=queued,
            bundles=bundles,
        )
        return Transition(next_state, action_id=_action_id(state, action))
    dispatching = replace(bundle, status="dispatching", due_at_ms=None)
    bundles = dict(state.bundles)
    bundles[bundle_id] = dispatching
    waiting = (
        state.bundle
        if state.next_bundle is not None
        and state.next_bundle.bundle_id == bundle_id
        and state.bundle is not None
        and state.bundle.bundle_id != bundle_id
        else None
    )
    turn = _reserved_turn(state, dispatching, now_ms)
    if turn.turn_id in state.turns:
        return Transition(state, action_id=_action_id(state, action))
    turns = dict(state.turns)
    turns[turn.turn_id] = turn
    next_state = _advance(
        state,
        now_ms,
        bundle=dispatching,
        next_bundle=(
            waiting
            if state.next_bundle is not None
            and state.next_bundle.bundle_id == bundle_id
            else state.next_bundle
        ),
        bundles=bundles,
        active_turn=turn,
        turns=turns,
        stop_requested=False,
    )
    effects = [_effect(state, action, 0, "cancel_timer", bundle_id=bundle_id)]
    effects.append(
        _effect(
            state,
            action,
            len(effects),
            "dispatch_turn",
            bundle_id=bundle_id,
            turn_id=turn.turn_id,
        )
    )
    return Transition(next_state, tuple(effects), _action_id(state, action))


def _cancel_bundle(state: BotState, action: ConversationAction) -> Transition:
    bundle_id = action.get("bundle_id")
    bundle = _live_bundle(state, bundle_id) if isinstance(bundle_id, str) else None
    if bundle is None or bundle.status not in {"open", "queued", "frozen"}:
        return Transition(state, action_id=_action_id(state, action))
    bundles = dict(state.bundles)
    bundles.pop(bundle_id, None)
    next_state = _advance(
        state,
        _now(state, action),
        bundle=(
            None
            if state.bundle is not None and state.bundle.bundle_id == bundle_id
            else state.bundle
        ),
        next_bundle=(
            None
            if state.next_bundle is not None
            and state.next_bundle.bundle_id == bundle_id
            else state.next_bundle
        ),
        bundles=bundles,
    )
    return Transition(
        next_state,
        (_effect(state, action, 0, "cancel_timer", bundle_id=bundle_id),),
        _action_id(state, action),
    )


def _hold_bundle(state: BotState, action: ConversationAction) -> Transition:
    bundle_id = action.get("bundle_id")
    bundle = _live_bundle(state, bundle_id) if isinstance(bundle_id, str) else None
    if bundle is None or bundle.status not in {"open", "queued"}:
        return Transition(state, action_id=_action_id(state, action))
    now_ms = _now(state, action)
    held = replace(
        bundle,
        held=True,
        due_at_ms=now_ms + HOLD_DELAY_MS,
        timer_generation=bundle.timer_generation + 1,
    )
    bundles = dict(state.bundles)
    bundles[bundle_id] = held
    next_state = _advance(
        state,
        now_ms,
        bundle=(
            held
            if state.bundle is not None and state.bundle.bundle_id == bundle_id
            else state.bundle
        ),
        next_bundle=(
            held
            if state.next_bundle is not None
            and state.next_bundle.bundle_id == bundle_id
            else state.next_bundle
        ),
        bundles=bundles,
    )
    return Transition(
        next_state,
        (
            _effect(state, action, 0, "cancel_timer", bundle_id=bundle_id),
            _effect(
                state,
                action,
                1,
                "schedule_timer",
                bundle_id=bundle_id,
                generation=held.timer_generation,
                due_at_ms=held.due_at_ms,
            ),
        ),
        _action_id(state, action),
    )


def _steer_current(state: BotState, action: ConversationAction) -> Transition:
    bundle_id = action.get("bundle_id")
    bundle = state.next_bundle
    if (
        not _has_active_lease(state)
        or state.active_turn.status != "active"
        or state.steering_bundle is not None
        or bundle is None
        or bundle.bundle_id != bundle_id
        or bundle.status not in {"open", "queued"}
    ):
        return Transition(state, action_id=_action_id(state, action))
    dispatching = replace(bundle, status="dispatching", due_at_ms=None)
    bundles = dict(state.bundles)
    bundles[bundle.bundle_id] = dispatching
    next_state = _advance(
        state,
        _now(state, action),
        next_bundle=None,
        steering_bundle=dispatching,
        bundles=bundles,
    )
    return Transition(
        next_state,
        (
            _effect(
                state,
                action,
                0,
                "steer_turn",
                turn_id=state.active_turn.turn_id,
                bundle_id=bundle.bundle_id,
            ),
        ),
        _action_id(state, action),
    )


def _stop(state: BotState, action: ConversationAction) -> Transition:
    active = state.active_turn
    if active is not None and active.status in {"configuring", "compacting"}:
        return Transition(
            state,
            action_id=_action_id(state, action),
            replies=("Wait for the current session operation to finish.",),
        )
    if active is None or state.stop_requested or active.status in {
        "aborted",
        "completed",
        "failed",
        "uncertain",
    }:
        return Transition(state, action_id=_action_id(state, action))
    turns = dict(state.turns)
    stopping = replace(active, status="stopping")
    turns[stopping.turn_id] = stopping
    queued = state.next_bundle
    bundles = dict(state.bundles)
    if queued is not None:
        queued = replace(queued, status="frozen", due_at_ms=None)
        bundles[queued.bundle_id] = queued
    next_state = _advance(
        state,
        _now(state, action),
        active_turn=stopping,
        next_bundle=queued,
        turns=turns,
        bundles=bundles,
        stop_requested=True,
    )
    return Transition(
        next_state,
        (_effect(state, action, 0, "abort_turn", turn_id=active.turn_id),),
        _action_id(state, action),
    )


def _new_session(state: BotState, action: ConversationAction) -> Transition:
    if _has_active_lease(state):
        return Transition(state, action_id=_action_id(state, action), replies=("Wait for the active Pi operation to finish before changing sessions.",))
    session_id = action.get("session_id")
    provider = action.get("provider")
    model_id = action.get("model_id")
    thinking = action.get("thinking")
    if not all(
        isinstance(value, str) and value
        for value in (session_id, provider, model_id, thinking)
    ):
        return Transition(state, action_id=_action_id(state, action))
    if session_id in state.pending_sessions:
        return Transition(state, action_id=_action_id(state, action))
    now_ms = _now(state, action)
    pending = dict(state.pending_sessions)
    try:
        pending[session_id] = PendingSession(
            SessionRef("pending", session_id),
            action.get("name"),
            SessionConfig(ModelRef(provider, model_id), thinking),
            now_ms,
            now_ms,
        )
    except (TypeError, ValueError):
        return Transition(state, action_id=_action_id(state, action))
    next_state = _advance(
        state,
        now_ms,
        selected_session_id=session_id,
        selected_session_path=None,
        pending_sessions=pending,
    )
    return Transition(next_state, action_id=_action_id(state, action))


def _use_session(state: BotState, action: ConversationAction) -> Transition:
    if _has_active_lease(state):
        return Transition(state, action_id=_action_id(state, action), replies=("Wait for the active Pi operation to finish before changing sessions.",))
    session_id = action.get("session_id") or action.selector
    native_path = action.get("native_path")
    pending = isinstance(session_id, str) and session_id in state.pending_sessions
    native = _native_session_marker(native_path, session_id)
    if not pending and not native:
        return Transition(state, action_id=_action_id(state, action))
    return Transition(
        _advance(
            state,
            _now(state, action),
            selected_session_id=session_id,
            selected_session_path=native_path if native else None,
        ),
        action_id=_action_id(state, action),
    )


def _configure_session(state: BotState, action: ConversationAction) -> Transition:
    session_id = action.get("session_id") or state.selected_session_id
    current = state.pending_sessions.get(session_id) if session_id else None
    if current is None:
        return Transition(state, action_id=_action_id(state, action))
    now_ms = _now(state, action)
    try:
        updated = replace(
            current,
            name=action.get("name", current.name),
            config=SessionConfig(
                ModelRef(
                    action.get("provider", current.provider),
                    action.get("model_id", current.model_id),
                ),
                action.get("thinking", current.thinking),
            ),
            updated_at_ms=now_ms,
        )
    except (TypeError, ValueError):
        return Transition(state, action_id=_action_id(state, action))
    pending = dict(state.pending_sessions)
    pending[session_id] = updated
    return Transition(
        _advance(state, now_ms, pending_sessions=pending),
        action_id=_action_id(state, action),
    )


def _begin_session_operation(
    state: BotState, action: ConversationAction
) -> Transition:
    operation_id = action.get("operation_id")
    operation_kind = action.get("operation_kind")
    session_id = action.get("session_id")
    if (
        not all(
            isinstance(value, str) and value
            for value in (operation_id, operation_kind, session_id)
        )
        or operation_kind not in {"configure", "compact"}
        or session_id != state.selected_session_id
    ):
        return Transition(state, action_id=_action_id(state, action))
    current_blocks_operation = state.bundle is not None and not (
        operation_kind == "configure" and state.bundle.status == "frozen"
    )
    if (
        _has_active_lease(state)
        or current_blocks_operation
        or state.next_bundle is not None
        or state.steering_bundle is not None
    ):
        return Transition(
            state,
            action_id=_action_id(state, action),
            replies=("Wait for the current or queued input to finish first.",),
        )
    if operation_kind == "compact" and state.selected_session_path is None:
        return Transition(
            state,
            action_id=_action_id(state, action),
            replies=("A pending session has nothing to compact yet.",),
        )
    now_ms = _now(state, action)
    operation = TurnRecord(
        operation_id,
        session_id,
        0,
        "configuring" if operation_kind == "configure" else "compacting",
        False,
        now_ms,
    )
    turns = dict(state.turns)
    if operation_id in turns:
        return Transition(state, action_id=_action_id(state, action))
    turns[operation_id] = operation
    return Transition(
        _advance(
            state,
            now_ms,
            active_turn=operation,
            active_turn_id=operation_id,
            turns=turns,
        ),
        (
            _effect(
                state,
                action,
                0,
                "run_session_operation",
                turn_id=operation_id,
                operation_kind=operation_kind,
            ),
        ),
        _action_id(state, action),
    )


def _session_operation_completed(
    state: BotState, action: ConversationAction
) -> Transition:
    turn_id = action.get("turn_id")
    active = state.active_turn
    if (
        not isinstance(turn_id, str)
        or active is None
        or active.turn_id != turn_id
        or active.status not in {"configuring", "compacting"}
    ):
        return Transition(state, action_id=_action_id(state, action))
    now_ms = _now(state, action)
    success = bool(action.get("success", False))
    turns = dict(state.turns)
    turns[turn_id] = replace(
        active,
        status="completed" if success else "failed",
        finished_at_ms=now_ms,
    )
    pending = dict(state.pending_sessions)
    if success and active.status == "configuring" and active.session_id in pending:
        current = pending[active.session_id]
        try:
            pending[active.session_id] = replace(
                current,
                config=SessionConfig(
                    ModelRef(
                        action.get("provider", current.provider),
                        action.get("model_id", current.model_id),
                    ),
                    action.get("thinking", current.thinking),
                ),
                updated_at_ms=now_ms,
            )
        except (TypeError, ValueError):
            success = False
            turns[turn_id] = replace(
                active,
                status="failed",
                finished_at_ms=now_ms,
            )
    bundles = dict(state.bundles)
    current_bundle = state.bundle
    queued = state.next_bundle
    dispatched, reserved = _reserve_due_queued_bundle(
        state,
        queued,
        bundles,
        turns,
        now_ms,
        enabled=True,
    )
    promote_waiting = (
        reserved is None
        and queued is not None
        and queued.status in {"open", "queued"}
        and queued.due_at_ms is not None
    )
    next_state = _advance(
        state,
        now_ms,
        pending_sessions=pending,
        active_turn=reserved,
        active_turn_id=reserved.turn_id if reserved is not None else None,
        bundle=(
            dispatched
            if reserved is not None
            else queued
            if promote_waiting
            else current_bundle
        ),
        next_bundle=None if reserved is not None or promote_waiting else queued,
        bundles=bundles,
        turns=turns,
    )
    effects: tuple[Effect, ...] = ()
    if reserved is not None and dispatched is not None:
        effects = (
            _effect(
                state,
                action,
                0,
                "dispatch_turn",
                bundle_id=dispatched.bundle_id,
                turn_id=reserved.turn_id,
            ),
        )
    message = action.get("message")
    reply = (
        message
        if isinstance(message, str) and 0 < len(message.encode("utf-8")) <= 300
        else "Session operation completed." if success else "Session operation failed."
    )
    return Transition(
        next_state,
        effects,
        _action_id(state, action),
        (reply,),
    )


def _materialize_session(state: BotState, action: ConversationAction) -> Transition:
    return _apply_materialization(state, action)


def _recover_materialization(state: BotState, action: ConversationAction) -> Transition:
    return _apply_materialization(state, action)


def _apply_materialization(
    state: BotState,
    action: ConversationAction,
) -> Transition:
    session_id = action.get("session_id")
    native_id = action.get("native_session_id")
    native_path = action.get("native_path")
    if (
        not isinstance(session_id, str)
        or session_id not in state.pending_sessions
        or not _native_session_marker(native_path, session_id)
        or native_id != session_id
    ):
        return Transition(state, action_id=_action_id(state, action))
    pending = dict(state.pending_sessions)
    del pending[session_id]
    return Transition(
        _advance(
            state,
            _now(state, action),
            selected_session_id=session_id,
            selected_session_path=native_path,
            pending_sessions=pending,
        ),
        action_id=_action_id(state, action),
    )


def _turn_accepted(state: BotState, action: ConversationAction) -> Transition:
    turn_id = action.get("turn_id")
    prior = state.turns.get(turn_id) if isinstance(turn_id, str) else None
    current = state.bundle
    matching_reservation = (
        prior is not None
        and prior.status in {"dispatching", "stopping"}
        and state.active_turn is not None
        and state.active_turn.turn_id == turn_id
        and current is not None
        and current.status == "dispatching"
    )
    session_id = (
        prior.session_id
        if matching_reservation
        else action.get("session_id") or state.selected_session_id
    )
    if (
        not isinstance(turn_id, str)
        or not turn_id
        or turn_id != turn_id.strip()
        or not isinstance(session_id, str)
        or not session_id
        or session_id != session_id.strip()
        or not matching_reservation
        or (
            state.blocking_ui is not None
            and state.blocking_ui.turn_id != turn_id
        )
        or (prior is not None and prior.status in TERMINAL_TURN_STATUSES)
    ):
        return Transition(state, action_id=_action_id(state, action))
    started_at_ms = (
        prior.started_at_ms
        if matching_reservation
        else action.get("started_at_ms", _now(state, action))
    )
    source_message_id = (
        prior.source_message_id
        if matching_reservation
        else action.get("source_message_id", 0)
    )
    if (
        type(started_at_ms) is not int
        or started_at_ms < 0
        or type(source_message_id) is not int
        or source_message_id < 0
    ):
        return Transition(state, action_id=_action_id(state, action))
    turn = TurnRecord(
        turn_id,
        session_id,
        source_message_id,
        "stopping" if matching_reservation and state.stop_requested else "active",
        True if matching_reservation else bool(action.get("prompt_accepted", True)),
        started_at_ms,
    )
    turns = dict(state.turns)
    turns[turn_id] = turn
    bundles = dict(state.bundles)
    if matching_reservation:
        bundles.pop(current.bundle_id, None)
    request_id = action.get("blocking_ui_request_id")
    existing_blocking = state.blocking_ui
    effects: list[Effect] = []
    if existing_blocking is not None and existing_blocking.turn_id == turn_id:
        blocking = existing_blocking
        generation = state.callback_generation
        if (
            isinstance(request_id, str)
            and request_id
            and request_id != existing_blocking.request_id
        ):
            effects.append(
                _effect(
                    state,
                    action,
                    0,
                    "cancel_ui",
                    request_id=request_id,
                    turn_id=turn_id,
                )
            )
    else:
        generation = (
            state.callback_generation + 1
            if isinstance(request_id, str) and request_id
            else state.callback_generation
        )
        blocking = (
            BlockingUiState(
                request_id,
                turn_id,
                _now(state, action) + UI_TIMEOUT_MS,
                action.get("callback_key", request_id),
                generation,
            )
            if isinstance(request_id, str) and request_id
            else existing_blocking
        )
    if state.stop_requested:
        effects.append(
            _effect(
                state,
                action,
                len(effects),
                "abort_turn",
                turn_id=turn_id,
            )
        )
    next_state = _advance(
        state,
        _now(state, action),
        active_turn=turn,
        turns=turns,
        bundle=None if matching_reservation else state.bundle,
        bundles=bundles,
        blocking_ui=blocking,
        callback_generation=generation,
        stop_requested=state.stop_requested if matching_reservation else False,
    )
    return Transition(
        next_state,
        tuple(effects),
        action_id=_action_id(state, action),
    )


def _blocking_ui_opened(state: BotState, action: ConversationAction) -> Transition:
    request_id = action.get("request_id")
    callback_key = action.get("callback_key")
    turn_id = action.get("turn_id")
    now_ms = _now(state, action)
    expires_at_ms = action.get("expires_at_ms", now_ms + UI_TIMEOUT_MS)
    if (
        not _has_active_lease(state)
        or not all(
            isinstance(value, str) and value
            for value in (request_id, callback_key, turn_id)
        )
        or state.active_turn.turn_id != turn_id
        or len(callback_key.encode("utf-8")) > 64
        or type(expires_at_ms) is not int
        or expires_at_ms <= now_ms
    ):
        return Transition(state, action_id=_action_id(state, action))
    if state.blocking_ui is not None:
        if state.blocking_ui.request_id == request_id:
            return Transition(state, action_id=_action_id(state, action))
        return Transition(
            _advance(state, now_ms),
            (
                _effect(
                    state,
                    action,
                    0,
                    "cancel_ui",
                    request_id=request_id,
                    turn_id=turn_id,
                ),
            ),
            _action_id(state, action),
        )
    generation = state.callback_generation + 1
    blocking = BlockingUiState(
        request_id,
        turn_id,
        expires_at_ms,
        callback_key,
        generation,
    )
    return Transition(
        _advance(
            state,
            now_ms,
            blocking_ui=blocking,
            callback_generation=generation,
        ),
        action_id=_action_id(state, action),
    )


def _answer_ui(state: BotState, action: ConversationAction) -> Transition:
    blocking = state.blocking_ui
    if (
        blocking is None
        or action.get("callback_key") != blocking.callback_key
        or action.get("generation") != blocking.generation
        or _now(state, action) >= blocking.expires_at_ms
        or not _has_active_lease(state)
        or state.active_turn.turn_id != blocking.turn_id
    ):
        return Transition(state, action_id=_action_id(state, action))
    response = action.get("response")
    if not isinstance(response, (str, bool)):
        return Transition(state, action_id=_action_id(state, action))
    next_state = _advance(
        state,
        _now(state, action),
        blocking_ui=None,
    )
    payload: dict[str, int | float | str | bool | None] = {
        "request_id": blocking.request_id,
        "turn_id": blocking.turn_id,
        "generation": blocking.generation,
    }
    payload["response"] = response
    return Transition(
        next_state,
        (_effect(state, action, 0, "answer_ui", **payload),),
        _action_id(state, action),
    )


def _blocking_ui_timeout(
    state: BotState,
    action: ConversationAction,
) -> Transition:
    blocking = state.blocking_ui
    now_ms = _now(state, action)
    if (
        blocking is None
        or action.get("callback_key") != blocking.callback_key
        or action.get("generation") != blocking.generation
        or now_ms < blocking.expires_at_ms
    ):
        return Transition(state, action_id=_action_id(state, action))
    return Transition(
        _advance(state, now_ms, blocking_ui=None),
        (
            _effect(
                state,
                action,
                0,
                "cancel_ui",
                request_id=blocking.request_id,
                turn_id=blocking.turn_id,
            ),
        ),
        _action_id(state, action),
    )


def _turn_completed(state: BotState, action: ConversationAction) -> Transition:
    turn_id = action.get("turn_id")
    status = action.get("status", "completed")
    if (
        not isinstance(turn_id, str)
        or not turn_id
        or turn_id != turn_id.strip()
        or status not in TERMINAL_TURN_STATUSES
    ):
        return Transition(state, action_id=_action_id(state, action))
    prior = state.turns.get(turn_id)
    if prior is not None and prior.status in TERMINAL_TURN_STATUSES:
        return Transition(state, action_id=_action_id(state, action))
    session_id = (
        prior.session_id
        if prior
        else action.get("session_id") or state.selected_session_id or "unselected"
    )
    source_message_id = (
        prior.source_message_id if prior else action.get("source_message_id", 0)
    )
    started_at_ms = (
        prior.started_at_ms
        if prior
        else action.get("started_at_ms", _now(state, action))
    )
    finished_at_ms = action.get("finished_at_ms", _now(state, action))
    if (
        type(source_message_id) is not int
        or source_message_id < 0
        or type(started_at_ms) is not int
        or type(finished_at_ms) is not int
        or started_at_ms < 0
        or finished_at_ms < started_at_ms
    ):
        return Transition(state, action_id=_action_id(state, action))
    turn = TurnRecord(
        turn_id,
        session_id,
        source_message_id,
        status,
        (prior.prompt_accepted or status != "rejected")
        if prior
        else status != "rejected",
        started_at_ms,
        finished_at_ms,
    )
    turns = dict(state.turns)
    turns[turn_id] = turn
    active = state.active_turn
    clear_active = active is not None and active.turn_id == turn_id
    current = state.bundle
    bundles = dict(state.bundles)
    rejected_before_acceptance = False
    if (
        clear_active
        and prior is not None
        and prior.status in {"dispatching", "stopping"}
        and current is not None
        and current.status == "dispatching"
    ):
        if status == "rejected" and not prior.prompt_accepted:
            current = _freeze_rejected_bundle(current)
            bundles[current.bundle_id] = current
            rejected_before_acceptance = True
        else:
            bundles.pop(current.bundle_id, None)
            current = None
    queued = state.next_bundle
    dispatched_queued, reserved = _reserve_due_queued_bundle(
        state,
        queued,
        bundles,
        turns,
        _now(state, action),
        enabled=clear_active,
    )
    dispatch_queued = reserved is not None
    promote_waiting = (
        clear_active
        and not dispatch_queued
        and current is None
        and queued is not None
        and queued.status in {"open", "queued"}
        and queued.due_at_ms is not None
        and not state.stop_requested
    )
    next_state = _advance(
        state,
        _now(state, action),
        active_turn=reserved if dispatch_queued else (None if clear_active else active),
        active_turn_id=(
            reserved.turn_id
            if dispatch_queued
            else (None if clear_active else state.active_turn_id)
        ),
        bundle=(
            dispatched_queued
            if dispatch_queued
            else (queued if promote_waiting else current)
        ),
        next_bundle=(
            None if dispatch_queued or promote_waiting else state.next_bundle
        ),
        bundles=bundles,
        turns=turns,
        blocking_ui=None if clear_active else state.blocking_ui,
        stop_requested=False if clear_active else state.stop_requested,
    )
    effects: list[Effect] = []
    delivery_pending = bool(action.get("delivery_pending", False))
    delivery_uncertain = bool(action.get("delivery_uncertain", False))
    delivery_expired = bool(action.get("delivery_expired", False))
    if source_message_id > 0 and not delivery_pending:
        effects.append(
            _effect(
                state,
                action,
                len(effects),
                "react",
                source_message_id=source_message_id,
                emoji=(
                    "😨"
                    if delivery_uncertain or delivery_expired
                    else "👌" if status in {"completed", "handled"} else "😨"
                ),
            )
        )
    replies: tuple[str, ...] = ()
    if delivery_pending:
        replies = (
            "Pi completed the work; Telegram delivery is pending and will retry automatically.",
        )
    elif delivery_expired:
        replies = ("Telegram delivery expired after 24 hours. Pi was not rerun.",)
    elif delivery_uncertain:
        replies = (
            "Pi completed the work, but Telegram delivery is uncertain and was not retried.",
        )
    if dispatch_queued:
        effects.append(
            _effect(
                state,
                action,
                len(effects),
                "dispatch_turn",
                bundle_id=dispatched_queued.bundle_id,
                turn_id=reserved.turn_id,
            )
        )
    return Transition(
        next_state,
        tuple(effects),
        _action_id(state, action),
        (REJECTED_PROMPT_REPLY,) if rejected_before_acceptance else replies,
    )


def _delivery_completed(state: BotState, action: ConversationAction) -> Transition:
    turn_id = action.get("turn_id")
    turn = state.turns.get(turn_id) if isinstance(turn_id, str) else None
    if turn is None or turn.status not in {"completed", "handled"}:
        return Transition(state, action_id=_action_id(state, action))
    effects: tuple[Effect, ...] = ()
    success = action.get("success", True) is True
    if turn.source_message_id > 0:
        effects = (
            _effect(
                state,
                action,
                0,
                "react",
                source_message_id=turn.source_message_id,
                emoji="👌" if success else "😨",
            ),
        )
    return Transition(
        _advance(state, _now(state, action)),
        effects,
        _action_id(state, action),
        replies=() if success else ("Telegram delivery expired after 24 hours. Pi was not rerun.",),
    )


def _bundle_dispatched(state: BotState, action: ConversationAction) -> Transition:
    bundle_id = action.get("bundle_id")
    turn_id = action.get("turn_id")
    bundle = state.bundle
    active = state.active_turn
    if (
        not isinstance(bundle_id, str)
        or not isinstance(turn_id, str)
        or bundle is None
        or bundle.bundle_id != bundle_id
        or bundle.status != "dispatching"
        or active is None
        or active.turn_id != turn_id
        or active.status not in {"dispatching", "stopping"}
        or active.turn_id in state.turns
        and state.turns[active.turn_id].status in TERMINAL_TURN_STATUSES
    ):
        return Transition(state, action_id=_action_id(state, action))
    now_ms = _now(state, action)
    bundles = dict(state.bundles)
    if not bool(action.get("accepted", False)):
        reason = action.get("reason")
        reply = (
            reason
            if isinstance(reason, str) and 0 < len(reason.encode("utf-8")) <= 300
            else REJECTED_PROMPT_REPLY
        )
        frozen = _freeze_rejected_bundle(bundle)
        bundles[bundle_id] = frozen
        turns = dict(state.turns)
        turns[turn_id] = replace(
            active,
            status="rejected",
            finished_at_ms=now_ms,
        )
        dispatched_queued, reserved = _reserve_due_queued_bundle(
            state,
            state.next_bundle,
            bundles,
            turns,
            now_ms,
            enabled=True,
        )
        next_state = _advance(
            state,
            now_ms,
            bundle=dispatched_queued or frozen,
            next_bundle=None if reserved is not None else state.next_bundle,
            bundles=bundles,
            active_turn=reserved,
            active_turn_id=reserved.turn_id if reserved is not None else None,
            turns=turns,
            stop_requested=False,
        )
        effects = [
            _effect(
                state,
                action,
                0,
                "react",
                source_message_id=active.source_message_id,
                emoji="😨",
            )
        ]
        if reserved is not None:
            effects.append(
                _effect(
                    state,
                    action,
                    len(effects),
                    "dispatch_turn",
                    bundle_id=dispatched_queued.bundle_id,
                    turn_id=reserved.turn_id,
                )
            )
        return Transition(
            next_state,
            tuple(effects),
            action_id=_action_id(state, action),
            replies=(reply,),
        )
    bundles.pop(bundle_id, None)
    turn = replace(
        active,
        status="stopping" if state.stop_requested else "active",
        prompt_accepted=True,
    )
    turns = dict(state.turns)
    turns[turn_id] = turn
    next_state = _advance(
        state,
        now_ms,
        bundle=None,
        bundles=bundles,
        active_turn=turn,
        turns=turns,
    )
    effects = (
        (
            _effect(
                state,
                action,
                0,
                "abort_turn",
                turn_id=turn_id,
            ),
        )
        if state.stop_requested
        else ()
    )
    return Transition(next_state, effects, action_id=_action_id(state, action))


def _steer_dispatched(state: BotState, action: ConversationAction) -> Transition:
    bundle_id = action.get("bundle_id")
    bundle = state.steering_bundle
    if (
        not isinstance(bundle_id, str)
        or bundle is None
        or bundle.bundle_id != bundle_id
        or bundle.status != "dispatching"
    ):
        return Transition(state, action_id=_action_id(state, action))
    bundles = dict(state.bundles)
    effects: list[Effect] = []
    if bool(action.get("accepted", False)):
        bundles.pop(bundle_id, None)
        queued = state.next_bundle
    else:
        later = state.next_bundle
        if later is not None:
            queued = replace(
                bundle,
                kind=(
                    "media"
                    if bundle.kind == "media" or later.kind == "media"
                    else "text"
                ),
                status="frozen" if state.stop_requested else "queued",
                items=(*bundle.items, *later.items),
                due_at_ms=None,
                timer_generation=max(
                    bundle.timer_generation,
                    later.timer_generation,
                )
                + 1,
                expires_at_ms=max(bundle.expires_at_ms, later.expires_at_ms),
            )
            bundles.pop(later.bundle_id, None)
            effects.append(
                _effect(
                    state,
                    action,
                    len(effects),
                    "cancel_timer",
                    bundle_id=later.bundle_id,
                )
            )
        else:
            queued = replace(
                bundle,
                status="frozen" if state.stop_requested else "queued",
                due_at_ms=None,
            )
        bundles[bundle_id] = queued
    return Transition(
        _advance(
            state,
            _now(state, action),
            next_bundle=queued,
            steering_bundle=None,
            bundles=bundles,
        ),
        tuple(effects),
        action_id=_action_id(state, action),
    )


def _artifact_queued(state: BotState, action: ConversationAction) -> Transition:
    artifact_id = action.get("artifact_id")
    path = action.get("staging_path")
    sha256 = action.get("sha256")
    size = action.get("size")
    if (
        not isinstance(artifact_id, str)
        or not artifact_id
        or artifact_id != artifact_id.strip()
        or not isinstance(path, str)
        or not path
        or not isinstance(sha256, str)
        or type(size) is not int
        or size < 0
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        return Transition(state, action_id=_action_id(state, action))
    now_ms = _now(state, action)
    artifacts = dict(state.artifacts)
    artifacts[artifact_id] = StoredArtifact(
        artifact_id,
        path,
        sha256,
        size,
        "pending",
        now_ms + PENDING_RETENTION_MS,
    )
    return Transition(
        _advance(state, now_ms, artifacts=artifacts),
        action_id=_action_id(state, action),
    )


def _recover(state: BotState, action: ConversationAction) -> Transition:
    now_ms = _now(state, action)
    pending_effects = _effect_sequence(action.get("pending_effects"))
    claimed_effects = _effect_sequence(action.get("claimed_effects"))
    settled: dict[str, str] = {}
    claimed_dispatch_turns: set[str] = set()

    for effect in claimed_effects:
        settled[effect.effect_id] = "failed"
        if effect.kind == "dispatch_turn":
            turn_id = effect.payload.get("turn_id")
            if isinstance(turn_id, str):
                claimed_dispatch_turns.add(turn_id)
    for effect in pending_effects:
        if effect.kind in PROCESS_EFFECT_KINDS - {"dispatch_turn"}:
            settled[effect.effect_id] = "failed"
        elif effect.kind in {"schedule_timer", "cancel_timer"}:
            settled[effect.effect_id] = "failed"

    turns = dict(state.turns)
    bundles = dict(state.bundles)
    active = state.active_turn
    uncertain_source_message_id = 0
    unsafe_turn = (
        active is not None
        and active.status not in TERMINAL_TURN_STATUSES
        and (
            active.status in {"active", "stopping", "configuring", "compacting"}
            or active.turn_id in claimed_dispatch_turns
        )
    )
    for effect in pending_effects:
        if effect.kind != "dispatch_turn":
            continue
        turn_id = effect.payload.get("turn_id")
        if (
            active is None
            or turn_id != active.turn_id
            or active.status != "dispatching"
        ):
            settled[effect.effect_id] = "failed"
    if unsafe_turn:
        uncertain = replace(active, status="uncertain", finished_at_ms=now_ms)
        turns[uncertain.turn_id] = uncertain
        uncertain_source_message_id = uncertain.source_message_id
        for effect in pending_effects:
            if (
                effect.kind == "dispatch_turn"
                and effect.payload.get("turn_id") == uncertain.turn_id
            ):
                settled[effect.effect_id] = "failed"
        current = state.bundle
        if current is not None and current.status == "dispatching":
            bundles.pop(current.bundle_id, None)
            current = None
        active = None
    else:
        current = state.bundle

    queued = state.next_bundle
    steering = state.steering_bundle
    if steering is not None:
        if queued is not None:
            bundles.pop(queued.bundle_id, None)
            steering = replace(
                steering,
                kind=(
                    "media"
                    if steering.kind == "media" or queued.kind == "media"
                    else "text"
                ),
                status="frozen",
                items=(*steering.items, *queued.items),
                due_at_ms=None,
                timer_generation=max(
                    steering.timer_generation,
                    queued.timer_generation,
                )
                + 1,
                expires_at_ms=max(steering.expires_at_ms, queued.expires_at_ms),
            )
        else:
            steering = replace(steering, status="frozen", due_at_ms=None)
        bundles[steering.bundle_id] = steering
        queued = steering
        steering = None
    if unsafe_turn and queued is not None:
        queued = replace(
            queued,
            status="frozen",
            due_at_ms=None,
            timer_generation=queued.timer_generation + 1,
        )
        bundles[queued.bundle_id] = queued

    effects: list[Effect] = []
    replies: list[str] = []
    if unsafe_turn:
        if uncertain_source_message_id > 0:
            effects.append(
                _effect(
                    state,
                    action,
                    len(effects),
                    "react",
                    source_message_id=uncertain_source_message_id,
                    emoji="😨",
                )
            )
        subject = (
            "session operation"
            if state.active_turn is not None
            and state.active_turn.status in {"configuring", "compacting"}
            else "Pi turn"
        )
        replies.append(
            f"The previous {subject}'s outcome is uncertain after restart and was "
            "not retried. Use Send or Cancel for any frozen queued input."
        )
    blocking = state.blocking_ui
    if blocking is not None:
        effects.append(
            _effect(
                state,
                action,
                len(effects),
                "cancel_ui",
                request_id=blocking.request_id,
                turn_id=blocking.turn_id,
            )
        )
        blocking = None

    for bundle in tuple(bundles.values()):
        if bundle.expires_at_ms <= now_ms:
            bundles.pop(bundle.bundle_id, None)
            if current is not None and current.bundle_id == bundle.bundle_id:
                current = None
            if queued is not None and queued.bundle_id == bundle.bundle_id:
                queued = None
            continue
        if (
            bundle.due_at_ms is not None
            and bundle.status in {"open", "queued"}
            and bundle in {current, queued}
        ):
            effects.append(
                _effect(
                    state,
                    action,
                    len(effects),
                    "schedule_timer",
                    bundle_id=bundle.bundle_id,
                    generation=bundle.timer_generation,
                    due_at_ms=bundle.due_at_ms,
                )
            )

    active, retired_reservation = _retire_unattempted_dispatch(
        active,
        current,
        turns,
        settled,
        pending_effects,
        now_ms,
    )

    changed = (
        unsafe_turn
        or retired_reservation
        or state.blocking_ui is not None
        or state.steering_bundle is not None
        or bundles != dict(state.bundles)
        or bool(effects)
        or bool(settled)
    )
    if not changed:
        return Transition(state, action_id=_action_id(state, action))
    next_state = _advance(
        state,
        now_ms,
        active_turn=active,
        active_turn_id=active.turn_id if active else None,
        blocking_ui=blocking,
        bundle=current,
        next_bundle=queued,
        steering_bundle=steering,
        bundles=bundles,
        turns=turns,
        stop_requested=False,
    )
    return Transition(
        next_state,
        tuple(effects),
        _action_id(state, action),
        tuple(replies),
        settled_effects=settled,
    )


def _prune(state: BotState, action: ConversationAction) -> Transition:
    now_ms = _now(state, action)
    pending_effects = _effect_sequence(action.get("pending_effects"))
    bundles = dict(state.bundles)
    turns = dict(state.turns)
    settled: dict[str, str] = {}
    active = state.active_turn
    current = state.bundle
    queued = state.next_bundle
    steering = state.steering_bundle
    pending_dispatch = {
        effect.payload.get("turn_id")
        for effect in pending_effects
        if effect.kind == "dispatch_turn"
    }

    for bundle in tuple(bundles.values()):
        if bundle.expires_at_ms > now_ms:
            continue
        is_unacknowledged_dispatch = (
            current is not None
            and current.bundle_id == bundle.bundle_id
            and active is not None
            and active.status == "dispatching"
        )
        if is_unacknowledged_dispatch and active.turn_id not in pending_dispatch:
            continue
        bundles.pop(bundle.bundle_id, None)
        if current is not None and current.bundle_id == bundle.bundle_id:
            current = None
        if queued is not None and queued.bundle_id == bundle.bundle_id:
            queued = None
        if steering is not None and steering.bundle_id == bundle.bundle_id:
            steering = None

    active, retired_reservation = _retire_unattempted_dispatch(
        active,
        current,
        turns,
        settled,
        pending_effects,
        now_ms,
    )
    effects: list[Effect] = []
    if (
        active is None
        and current is None
        and queued is not None
        and queued.status in {"open", "queued"}
        and not state.stop_requested
    ):
        current = queued
        queued = None
        if current.due_at_ms is None:
            current = replace(
                current,
                status="open",
                due_at_ms=now_ms,
                timer_generation=current.timer_generation + 1,
            )
            bundles[current.bundle_id] = current
            effects.append(
                _effect(
                    state,
                    action,
                    0,
                    "schedule_timer",
                    bundle_id=current.bundle_id,
                    generation=current.timer_generation,
                    due_at_ms=current.due_at_ms,
                )
            )

    changed = (
        bundles != dict(state.bundles)
        or turns != dict(state.turns)
        or active != state.active_turn
        or current != state.bundle
        or queued != state.next_bundle
        or steering != state.steering_bundle
        or retired_reservation
        or bool(effects)
        or bool(settled)
    )
    if not changed:
        return Transition(state, action_id=_action_id(state, action))
    return Transition(
        _advance(
            state,
            now_ms,
            active_turn=active,
            active_turn_id=active.turn_id if active else None,
            bundle=current,
            next_bundle=queued,
            steering_bundle=steering,
            bundles=bundles,
            turns=turns,
            stop_requested=False if retired_reservation else state.stop_requested,
        ),
        tuple(effects),
        _action_id(state, action),
        settled_effects=settled,
    )


def _retire_unattempted_dispatch(
    active: TurnRecord | None,
    current: InputBundle | None,
    turns: dict[str, TurnRecord],
    settled: dict[str, str],
    pending_effects: tuple[Effect, ...],
    now_ms: int,
) -> tuple[TurnRecord | None, bool]:
    if active is None or active.status != "dispatching" or current is not None:
        return active, False
    matches = tuple(
        effect
        for effect in pending_effects
        if effect.kind == "dispatch_turn"
        and effect.payload.get("turn_id") == active.turn_id
    )
    if not matches:
        return active, False
    turns[active.turn_id] = replace(
        active,
        status="rejected",
        finished_at_ms=now_ms,
    )
    for effect in matches:
        settled[effect.effect_id] = "failed"
    return None, True


def _freeze_rejected_bundle(bundle: InputBundle) -> InputBundle:
    return replace(
        bundle,
        status="frozen",
        due_at_ms=None,
        timer_generation=bundle.timer_generation + 1,
    )


def _reserve_due_queued_bundle(
    state: BotState,
    queued: InputBundle | None,
    bundles: dict[str, InputBundle],
    turns: dict[str, TurnRecord],
    now_ms: int,
    *,
    enabled: bool,
) -> tuple[InputBundle | None, TurnRecord | None]:
    if (
        not enabled
        or queued is None
        or queued.status != "queued"
        or queued.due_at_ms is not None
        or state.stop_requested
    ):
        return None, None
    dispatching = replace(queued, status="dispatching")
    reserved = _reserved_turn(state, dispatching, now_ms)
    if reserved.turn_id in turns:
        return None, None
    bundles[dispatching.bundle_id] = dispatching
    turns[reserved.turn_id] = reserved
    return dispatching, reserved


def _advance(state: BotState, now_ms: int, **changes: Any) -> BotState:
    return replace(
        state,
        version=state.version + 1,
        now_ms=max(state.now_ms, now_ms),
        **changes,
    )


def _live_bundle(state: BotState, bundle_id: str) -> InputBundle | None:
    for bundle in (state.bundle, state.next_bundle):
        if bundle is not None and bundle.bundle_id == bundle_id:
            return bundle
    return state.bundles.get(bundle_id)


def _now(state: BotState, action: ConversationAction) -> int:
    value = action.get("now_ms", state.now_ms)
    return value if isinstance(value, int) and value >= 0 else state.now_ms


def _action_id(state: BotState, action: ConversationAction) -> str:
    supplied = action.get("action_id")
    if isinstance(supplied, str) and supplied:
        return supplied
    source_message_id = action.get("source_message_id")
    if action.kind in INPUT_ACTION_KINDS and source_message_id is not None:
        return (
            f"{action.kind}:chat_id={state.chat_id}:"
            f"source_message_id={source_message_id}"
        )
    for name in ("update_id", "callback_query_id"):
        telegram_id = action.get(name)
        if telegram_id is not None:
            return f"{action.kind}:{name}={telegram_id}"
    identity_parts = tuple(
        f"{name}={action.get(name)}"
        for name in (
            "source_message_id",
            "bundle_id",
            "turn_id",
            "session_id",
            "artifact_id",
            "request_id",
            "callback_key",
            "generation",
            "status",
            "accepted",
        )
        if action.get(name) is not None
    )
    identity = "|".join(identity_parts) if identity_parts else str(state.version)
    return f"{action.kind}:{state.version}:{identity}:{_now(state, action)}"


def _effect(
    state: BotState,
    action: ConversationAction,
    index: int,
    kind: str,
    **payload: int | float | str | bool | None,
) -> Effect:
    action_id = _action_id(state, action)
    return Effect(
        kind=kind,
        effect_id=f"{action_id}:{index}:{kind}",
        payload=payload,
    )


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _has_active_lease(state: BotState) -> bool:
    return state.active_turn is not None and state.active_turn.status in {
        "dispatching",
        "active",
        "stopping",
        "configuring",
        "compacting",
    }


def _native_session_marker(value: object, session_id: object) -> bool:
    return (
        isinstance(value, str)
        and isinstance(session_id, str)
        and value == session_id
        and bool(value)
    )


def _reserved_turn(
    state: BotState,
    bundle: InputBundle,
    now_ms: int,
) -> TurnRecord:
    return TurnRecord(
        (
            f"dispatch-{bundle.bundle_id}-"
            f"{bundle.timer_generation}"
        ),
        state.selected_session_id or "unselected",
        bundle.items[0].source_message_id,
        "dispatching",
        False,
        now_ms,
    )


def _effect_sequence(value: Any) -> tuple[Effect, ...]:
    if not isinstance(value, tuple) or not all(
        isinstance(effect, Effect) for effect in value
    ):
        return ()
    return value
