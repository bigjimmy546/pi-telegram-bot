"""Ordered execution of persisted coordinator effects."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from telegram_pi_bot.media import Attachment, AttachmentStore
from telegram_pi_bot.model import (
    ConversationAction,
    Effect,
    NativeSession,
    NativeSessionRef,
    RuntimeEvent,
    RuntimeEventKind,
    SessionConfigChange,
    SessionConfig,
    TurnContent,
    TurnRequest,
    TurnResult,
    TurnStatus,
    UiResponse,
)
from telegram_pi_bot.outbound import DeliveryQueue, OutboundPort
from telegram_pi_bot.pi_runtime import ActiveTurn
from telegram_pi_bot.store import ControlStore
from telegram_pi_bot.telegram_ui import ProgressRenderer, callback_data


Dispatch = Callable[[ConversationAction], Awaitable[None]]


class Clock(Protocol):
    def now_ms(self) -> int: ...

    async def sleep_until(self, due_at_ms: int) -> None: ...


class RuntimePort(Protocol):
    async def start_turn(
        self,
        request: TurnRequest,
        events: Callable[[RuntimeEvent], Awaitable[None]],
    ) -> ActiveTurn: ...


class EffectTelegramPort(OutboundPort, Protocol):
    async def react(self, chat_id: int, message_id: int, emoji: str) -> None: ...

    async def send_choices(
        self,
        chat_id: int,
        text: str,
        choices: tuple[tuple[str, str], ...],
    ) -> int: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    async def sleep_until(self, due_at_ms: int) -> None:
        delay = max(0.0, (due_at_ms - self.now_ms()) / 1_000)
        await asyncio.sleep(delay)


@dataclass(slots=True)
class _Timer:
    effect_id: str
    task: asyncio.Task[None]


@dataclass(slots=True)
class _Turn:
    active: ActiveTurn
    bundle_id: str
    attachments: tuple[Attachment, ...]


@dataclass(frozen=True, slots=True)
class _SessionOperation:
    kind: str
    mode: str | None = None
    selector: str | None = None
    instructions: str | None = None
    agy_provider: str = ""
    agy_model_id: str = ""
    agy_thinking: str = "high"


class EffectRunner:
    """Map exact persisted effect kinds to their owning adapter."""

    def __init__(
        self,
        *,
        chat_id: int,
        store: ControlStore,
        runtime: RuntimePort,
        telegram: EffectTelegramPort,
        delivery: DeliveryQueue,
        progress: ProgressRenderer,
        attachment_store: AttachmentStore,
        attachments: dict[str, Attachment] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.chat_id = chat_id
        self.store = store
        self.runtime = runtime
        self.telegram = telegram
        self.delivery = delivery
        self.progress = progress
        self.attachment_store = attachment_store
        self.attachments = attachments if attachments is not None else {}
        self.clock = clock or SystemClock()
        self._dispatch: Dispatch | None = None
        self._timers: dict[str, _Timer] = {}
        self._turns: dict[str, _Turn] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._ui_responses: dict[tuple[str, int, str], str | bool] = {}
        self._ui_keys: dict[str, tuple[str, int]] = {}
        self._ui_timers: dict[str, asyncio.Task[None]] = {}
        self._delivery_retries: dict[str, asyncio.Task[None]] = {}
        self._session_operations: dict[str, _SessionOperation] = {}

    def bind(self, dispatch: Dispatch) -> None:
        if self._dispatch is not None:
            raise RuntimeError("effect runner is already bound")
        self._dispatch = dispatch

    async def run(self, effect: Effect) -> None:
        """Start one claimed effect; deferred effects settle through actions."""

        handlers = {
            "schedule_timer": self._schedule_timer,
            "cancel_timer": self._cancel_timer,
            "react": self._react,
            "dispatch_turn": self._dispatch_turn,
            "steer_turn": self._steer_turn,
            "abort_turn": self._abort_turn,
            "answer_ui": self._answer_ui,
            "cancel_ui": self._cancel_ui,
            "run_session_operation": self._run_session_operation,
        }
        handler = handlers.get(effect.kind)
        if handler is None:
            self.store.mark_effect(effect.effect_id, "failed")
            return
        try:
            await handler(effect)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.store.mark_effect(effect.effect_id, "failed")

    async def close(self) -> None:
        for timer in tuple(self._timers.values()):
            timer.task.cancel()
        for task in tuple(self._ui_timers.values()):
            task.cancel()
        for turn in tuple(self._turns.values()):
            await turn.active.close()
        result_tasks = tuple(
            task
            for task in self._tasks
            if task.get_name().startswith("pi-result-") and not task.done()
        )
        if result_tasks:
            try:
                async with asyncio.timeout(30):
                    await asyncio.gather(*result_tasks, return_exceptions=True)
            except TimeoutError:
                for task in result_tasks:
                    task.cancel()
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._timers.clear()
        self._ui_timers.clear()
        self._ui_keys.clear()
        self._ui_responses.clear()
        self._delivery_retries.clear()
        self._session_operations.clear()
        self._turns.clear()

    def attachment(self, path: str) -> Attachment | None:
        return self.attachments.get(path)

    def active_attachment_paths(self) -> tuple[str, ...]:
        return tuple(str(attachment.path) for turn in self._turns.values() for attachment in turn.attachments)

    def ui_response(
        self, action: str, key: str, generation: object
    ) -> str | bool | None:
        if type(generation) is not int:
            return None
        return self._ui_responses.get((key, generation, action))

    def resume_delivery(self, delivery_id: str) -> None:
        if delivery_id in self._delivery_retries:
            return
        status = self.delivery.status(delivery_id)
        if status.status != "pending":
            return
        task = self._spawn(
            self._retry_delivery(delivery_id, status.turn_id),
            f"telegram-delivery-{delivery_id}",
        )
        self._delivery_retries[delivery_id] = task

    async def notice_expired_delivery(self, delivery_id: str) -> None:
        status = self.delivery.status(delivery_id)
        if status.status == "expired":
            await self._emit(ConversationAction(
                "delivery_completed", success=False,
                action_id=f"delivery_expired:{delivery_id}",
                delivery_id=delivery_id, turn_id=status.turn_id,
                now_ms=self.clock.now_ms(),
            ))

    def new_pending_session(self, name: str | None = None):
        return self.runtime.new_pending_session(name)

    async def inspect(self, session: NativeSessionRef | None, *, config: SessionConfig | None = None):
        if config is not None:
            return await self.runtime.inspect(session, config=config)
        return await self.runtime.inspect(session)

    async def list_sessions(self, limit: int | None):
        return await self.runtime.list_sessions(limit)

    async def resolve_skill(self, name: str):
        return await self.runtime.resolve_skill(name)

    def stage_session_operation(
        self,
        operation_id: str,
        *,
        kind: str,
        mode: str | None = None,
        selector: str | None = None,
        instructions: str | None = None,
        agy_provider: str = "",
        agy_model_id: str = "",
        agy_thinking: str = "high",
    ) -> None:
        if operation_id in self._session_operations:
            raise ValueError("session operation is already staged")
        self._session_operations[operation_id] = _SessionOperation(
            kind,
            mode,
            selector,
            instructions,
            agy_provider,
            agy_model_id,
            agy_thinking,
        )

    def discard_session_operation(self, operation_id: str) -> None:
        self._session_operations.pop(operation_id, None)

    async def _schedule_timer(self, effect: Effect) -> None:
        bundle_id = _text(effect, "bundle_id")
        due_at_ms = _integer(effect, "due_at_ms")
        generation = _integer(effect, "generation")
        prior = self._timers.pop(bundle_id, None)
        if prior is not None:
            prior.task.cancel()
            self.store.mark_effect(prior.effect_id, "failed")
        task = self._spawn(
            self._fire_timer(effect, bundle_id, generation, due_at_ms),
            f"bundle-timer-{bundle_id}",
        )
        self._timers[bundle_id] = _Timer(effect.effect_id, task)

    async def _fire_timer(
        self,
        effect: Effect,
        bundle_id: str,
        generation: int,
        due_at_ms: int,
    ) -> None:
        try:
            await self.clock.sleep_until(due_at_ms)
            await self._emit(
                ConversationAction(
                    "bundle_timer_fired",
                    bundle_id=bundle_id,
                    generation=generation,
                    now_ms=self.clock.now_ms(),
                    caused_by_effect_id=effect.effect_id,
                )
            )
            self.store.mark_effect(effect.effect_id, "done")
        except asyncio.CancelledError:
            self.store.mark_effect(effect.effect_id, "failed")
        finally:
            current = self._timers.get(bundle_id)
            if current is not None and current.effect_id == effect.effect_id:
                self._timers.pop(bundle_id, None)

    async def _cancel_timer(self, effect: Effect) -> None:
        bundle_id = _text(effect, "bundle_id")
        timer = self._timers.pop(bundle_id, None)
        if timer is not None:
            timer.task.cancel()
            self.store.mark_effect(timer.effect_id, "failed")
        self.store.mark_effect(effect.effect_id, "done")

    async def _react(self, effect: Effect) -> None:
        await self.telegram.react(
            self.chat_id,
            _integer(effect, "source_message_id"),
            _text(effect, "emoji"),
        )
        self.store.mark_effect(effect.effect_id, "done")

    async def _dispatch_turn(self, effect: Effect) -> None:
        self._spawn(self._begin_turn(effect), f"pi-{_text(effect, 'turn_id')}")

    async def _begin_turn(self, effect: Effect) -> None:
        turn_id = _text(effect, "turn_id")
        bundle_id = _text(effect, "bundle_id")
        state = self.store.load(self.chat_id)
        turn_record = state.active_turn
        bundle = state.bundle
        if (
            turn_record is None
            or turn_record.turn_id != turn_id
            or turn_record.status not in {"dispatching", "stopping"}
            or bundle is None
            or bundle.bundle_id != bundle_id
            or bundle.status != "dispatching"
        ):
            self.store.mark_effect(effect.effect_id, "failed")
            return
        session = _selected_session(state)
        if session is None:
            await self._reject_dispatch(effect, bundle_id, turn_id)
            return
        attachments = tuple(
            item
            for item in (
                self.attachment(str(bundle_item.value))
                for bundle_item in bundle.items
                if bundle_item.kind in {"photo", "document"}
            )
            if item is not None
        )
        content = _turn_content(bundle.items)
        buffered: list[RuntimeEvent] = []
        live = False

        async def receive(event: RuntimeEvent) -> None:
            if live:
                await self._runtime_event(turn_id, event)
            else:
                buffered.append(event)

        try:
            active = await self.runtime.start_turn(TurnRequest(session, content), receive)
        except Exception:
            await self._reject_dispatch(effect, bundle_id, turn_id)
            return
        if not active.accepted:
            result = await active.wait()
            await active.close()
            await self._reject_dispatch(
                effect,
                bundle_id,
                turn_id,
                reason=result.text,
            )
            return
        self._turns[turn_id] = _Turn(active, bundle_id, attachments)
        await self._emit(
            ConversationAction(
                "bundle_dispatched",
                bundle_id=bundle_id,
                turn_id=turn_id,
                accepted=True,
                now_ms=self.clock.now_ms(),
                caused_by_effect_id=effect.effect_id,
            )
        )
        self.store.mark_effect(effect.effect_id, "done")
        live = True
        for event in buffered:
            await self._runtime_event(turn_id, event)
        self._spawn(self._finish_turn(turn_id), f"pi-result-{turn_id}")

    async def _reject_dispatch(
        self,
        effect: Effect,
        bundle_id: str,
        turn_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        await self._emit(
            ConversationAction(
                "bundle_dispatched",
                bundle_id=bundle_id,
                turn_id=turn_id,
                accepted=False,
                reason=reason,
                now_ms=self.clock.now_ms(),
                caused_by_effect_id=effect.effect_id,
            )
        )
        self.store.mark_effect(effect.effect_id, "done")

    async def _runtime_event(self, turn_id: str, event: RuntimeEvent) -> None:
        if event.kind in {
            RuntimeEventKind.PROGRESS,
            RuntimeEventKind.TOOL_ACTIVITY,
            RuntimeEventKind.WARNING,
        }:
            await self.progress.publish(self.chat_id, event, now_ms=self.clock.now_ms())
            return
        if event.kind is not RuntimeEventKind.UI_REQUEST or event.ui_request is None:
            return
        request = event.ui_request
        if request.kind not in {"select", "confirm"}:
            return
        key = secrets.token_hex(8)
        await self._emit(
            ConversationAction(
                "blocking_ui_opened",
                request_id=request.request_id,
                callback_key=key,
                turn_id=turn_id,
                expires_at_ms=self.clock.now_ms() + min(request.timeout_ms or 600_000, 600_000),
                now_ms=self.clock.now_ms(),
            )
        )
        state = self.store.load(self.chat_id)
        blocking = state.blocking_ui
        if blocking is None or blocking.callback_key != key:
            await self._turns[turn_id].active.cancel_ui(request.request_id)
            return
        self._ui_keys[request.request_id] = (key, blocking.generation)
        self._ui_timers[request.request_id] = self._spawn(
            self._expire_ui(
                request.request_id,
                key,
                blocking.generation,
                blocking.expires_at_ms,
            ),
            f"pi-ui-{request.request_id}",
        )
        if request.kind == "confirm":
            self._ui_responses[(key, blocking.generation, "ui_yes")] = True
            self._ui_responses[(key, blocking.generation, "ui_no")] = False
            choices = (
                ("Yes", callback_data("ui_yes", key, blocking.generation)),
                ("No", callback_data("ui_no", key, blocking.generation)),
            )
        else:
            choices_list: list[tuple[str, str]] = []
            for index, option in enumerate(request.options[:10]):
                action = f"ui_{index}"
                self._ui_responses[(key, blocking.generation, action)] = option
                choices_list.append(
                    (option[:64], callback_data(action, key, blocking.generation))
                )
            choices = tuple(choices_list)
        try:
            show_ui = getattr(self.telegram, "show_ui", None)
            if callable(show_ui):
                await show_ui(request, ("ui_answer", key, blocking.generation))
            else:
                await self.telegram.send_choices(
                    self.chat_id, request.title[:512], choices
                )
        except Exception:
            await self._turns[turn_id].active.cancel_ui(request.request_id)
            self._forget_ui(request.request_id)

    async def _expire_ui(
        self,
        request_id: str,
        key: str,
        generation: int,
        expires_at_ms: int,
    ) -> None:
        try:
            await self.clock.sleep_until(expires_at_ms)
            await self._emit(
                ConversationAction(
                    "blocking_ui_timeout",
                    callback_key=key,
                    generation=generation,
                    now_ms=self.clock.now_ms(),
                )
            )
        except asyncio.CancelledError:
            return
        finally:
            self._ui_timers.pop(request_id, None)
            self._forget_ui(request_id)

    async def _finish_turn(self, turn_id: str) -> None:
        owned = self._turns.get(turn_id)
        if owned is None:
            return
        try:
            result = await owned.active.wait()
        except Exception:
            result = TurnResult(TurnStatus.UNCERTAIN, "Pi turn outcome is uncertain.")
        record = self.store.load(self.chat_id).turns.get(turn_id)
        settled_stamp = await self._native_stamp(record.session_id) if record else None
        delivered = True
        delivery_pending = False
        delivery_uncertain = False
        delivery_expired = False
        if result.text is not None or result.artifacts or result.status in {
            TurnStatus.COMPLETED,
            TurnStatus.HANDLED,
        }:
            delivery_id = self.delivery.enqueue(
                turn_id=turn_id,
                chat_id=self.chat_id,
                text=result.text,
                artifacts=result.artifacts,
                now_ms=self.clock.now_ms(),
            )
            delivered = await self.delivery.deliver(
                delivery_id, self.telegram, now_ms=self.clock.now_ms()
            )
            if not delivered:
                delivery_status = self.delivery.status(delivery_id)
                delivery_pending = delivery_status.status == "pending"
                delivery_uncertain = delivery_status.status == "uncertain"
                delivery_expired = delivery_status.status == "expired"
                if delivery_pending:
                    self.resume_delivery(delivery_id)
        status = result.status.value
        if result.materialized_session is not None:
            await self._emit(
                ConversationAction(
                    "materialized_session",
                    session_id=result.materialized_session.ref.id,
                    native_session_id=result.materialized_session.ref.id,
                    native_path=result.materialized_session.ref.id,
                    now_ms=self.clock.now_ms(),
                )
            )
        if settled_stamp is not None and record is not None:
            try:
                current_stamp = await self._native_stamp(record.session_id)
                if current_stamp is not None and current_stamp != settled_stamp:
                    await self.telegram.send_text(self.chat_id, "Native session changed after the bot's Pi process settled. Share sequentially with terminal Pi; this best-effort check does not lock terminal Pi.")
            except Exception:
                pass
        await self._emit(
            ConversationAction(
                "turn_completed",
                turn_id=turn_id,
                status=status,
                delivery_pending=delivery_pending,
                delivery_uncertain=delivery_uncertain,
                delivery_expired=delivery_expired,
                now_ms=self.clock.now_ms(),
            )
        )
        self.progress.reset(self.chat_id)
        self._turns.pop(turn_id, None)
        for task in tuple(self._ui_timers.values()):
            task.cancel()
        self._ui_timers.clear()
        self._ui_keys.clear()
        self._ui_responses.clear()
        for attachment in owned.attachments:
            try:
                self.attachment_store.release(attachment)
            except Exception:
                pass
            self.attachments.pop(str(attachment.path), None)

    async def _native_stamp(self, session_id: str) -> int | None:
        try:
            async with asyncio.timeout(10):
                sessions = await self.runtime.list_sessions(50)
            return next((session.updated_at_ms for session in sessions if session.ref.id == session_id), None)
        except Exception:
            return None

    async def _retry_delivery(self, delivery_id: str, turn_id: str) -> None:
        delay_ms = 5_000
        try:
            while self.delivery.status(delivery_id).status == "pending":
                await self.clock.sleep_until(self.clock.now_ms() + delay_ms)
                delivered = await self.delivery.deliver(
                    delivery_id,
                    self.telegram,
                    now_ms=self.clock.now_ms(),
                )
                if delivered:
                    await self._emit(
                        ConversationAction(
                            "delivery_completed",
                            action_id=f"delivery_completed:{delivery_id}",
                            delivery_id=delivery_id,
                            turn_id=turn_id,
                            now_ms=self.clock.now_ms(),
                        )
                    )
                    return
                delay_ms = min(delay_ms * 2, 5 * 60 * 1_000)
            await self.notice_expired_delivery(delivery_id)
        except asyncio.CancelledError:
            return
        finally:
            self._delivery_retries.pop(delivery_id, None)

    async def _run_session_operation(self, effect: Effect) -> None:
        turn_id = _text(effect, "turn_id")
        self._spawn(
            self._execute_session_operation(effect, turn_id),
            f"pi-session-operation-{turn_id}",
        )

    async def _execute_session_operation(
        self, effect: Effect, turn_id: str
    ) -> None:
        operation = self._session_operations.pop(turn_id, None)
        state = self.store.load(self.chat_id)
        active = state.active_turn
        if (
            operation is None
            or active is None
            or active.turn_id != turn_id
            or active.status not in {"configuring", "compacting"}
        ):
            await self._complete_session_operation(
                effect,
                turn_id,
                success=False,
                message="Session operation could not start safely.",
            )
            return
        try:
            if operation.kind == "compact":
                result = await self.runtime.compact(
                    NativeSessionRef(active.session_id), operation.instructions
                )
                if not result.success:
                    raise RuntimeError("compaction did not succeed")
                await self._complete_session_operation(
                    effect,
                    turn_id,
                    success=True,
                    message=(
                        f"Compacted: {result.tokens_before} → "
                        f"{result.estimated_tokens_after} estimated tokens."
                    ),
                )
                return
            session = (
                NativeSessionRef(active.session_id)
                if state.selected_session_path is not None
                else None
            )
            pending = state.pending_sessions.get(active.session_id) if session is None else None
            snapshot = await self.inspect(session, config=pending.config if pending else None)
            if operation.mode == "model":
                matches = [
                    model
                    for model in snapshot.models
                    if operation.selector
                    in {model.model_id, f"{model.provider}/{model.model_id}"}
                ]
                if len(matches) != 1:
                    raise LookupError("model selector is unavailable or ambiguous")
                model = matches[0]
                thinking = (
                    operation.agy_thinking
                    if (
                        model.provider == operation.agy_provider
                        and model.model_id == operation.agy_model_id
                    )
                    else None
                )
                change = SessionConfigChange(model=model, thinking=thinking)
                if pending is not None:
                    proposed = thinking or pending.thinking
                    levels = (await self.inspect(None, config=SessionConfig(model, proposed))).thinking_levels
                    if thinking is not None and thinking not in levels:
                        raise LookupError("model thinking profile unavailable")
                    if thinking is None and proposed not in levels:
                        if not levels:
                            raise LookupError("model thinking levels unavailable")
                        thinking = "medium" if "medium" in levels else levels[0]
            elif operation.mode == "thinking":
                if operation.selector not in snapshot.thinking_levels:
                    raise LookupError("thinking level is unavailable")
                model = None
                thinking = operation.selector
                change = SessionConfigChange(thinking=thinking)
            else:
                raise ValueError("session configuration request is invalid")
            if session is not None:
                applied = await self.runtime.configure_session(session, change)
                model = applied.selected_model
                thinking = applied.thinking
            else:
                pending = state.pending_sessions.get(active.session_id)
                if pending is None:
                    raise LookupError("pending session is unavailable")
                model = model or pending.config.model
                thinking = thinking or pending.thinking
            if model is None or thinking is None:
                raise ValueError("session configuration result is invalid")
            await self._complete_session_operation(
                effect,
                turn_id,
                success=True,
                message="Session configuration updated.",
                provider=model.provider,
                model_id=model.model_id,
                thinking=thinking,
            )
        except Exception:
            await self._complete_session_operation(
                effect,
                turn_id,
                success=False,
                message="Session operation failed without changing queued input.",
            )

    async def _complete_session_operation(
        self,
        effect: Effect,
        turn_id: str,
        *,
        success: bool,
        message: str,
        provider: str | None = None,
        model_id: str | None = None,
        thinking: str | None = None,
    ) -> None:
        await self._emit(
            ConversationAction(
                "session_operation_completed",
                turn_id=turn_id,
                success=success,
                message=message,
                provider=provider,
                model_id=model_id,
                thinking=thinking,
                now_ms=self.clock.now_ms(),
                caused_by_effect_id=effect.effect_id,
            )
        )

    async def _steer_turn(self, effect: Effect) -> None:
        turn_id = _text(effect, "turn_id")
        bundle_id = _text(effect, "bundle_id")
        owned = self._turns.get(turn_id)
        state = self.store.load(self.chat_id)
        bundle = state.steering_bundle
        accepted = False
        if owned is not None and bundle is not None and bundle.bundle_id == bundle_id:
            try:
                await owned.active.steer(_turn_content(bundle.items))
                accepted = True
            except Exception:
                accepted = False
        await self._emit(
            ConversationAction(
                "steer_dispatched",
                bundle_id=bundle_id,
                accepted=accepted,
                now_ms=self.clock.now_ms(),
                caused_by_effect_id=effect.effect_id,
            )
        )
        self.store.mark_effect(effect.effect_id, "done")

    async def _abort_turn(self, effect: Effect) -> None:
        turn_id = _text(effect, "turn_id")
        owned = self._turns.get(turn_id)
        if owned is None:
            self.store.mark_effect(effect.effect_id, "failed")
            return
        await owned.active.abort()
        self.store.mark_effect(effect.effect_id, "done")

    async def _answer_ui(self, effect: Effect) -> None:
        turn_id = _text(effect, "turn_id")
        owned = self._turns.get(turn_id)
        if owned is None:
            self.store.mark_effect(effect.effect_id, "failed")
            return
        request_id = _text(effect, "request_id")
        timer = self._ui_timers.pop(request_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        await owned.active.answer_ui(request_id, UiResponse(effect.payload.get("response")))
        self._forget_ui(request_id)
        self.store.mark_effect(effect.effect_id, "done")

    async def _cancel_ui(self, effect: Effect) -> None:
        turn_id = _text(effect, "turn_id")
        request_id = _text(effect, "request_id")
        timer = self._ui_timers.pop(request_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        owned = self._turns.get(turn_id)
        if owned is not None:
            await owned.active.cancel_ui(request_id)
        self._forget_ui(request_id)
        self.store.mark_effect(effect.effect_id, "done")

    def _forget_ui(self, request_id: str) -> None:
        identity = self._ui_keys.pop(request_id, None)
        if identity is None:
            return
        key, generation = identity
        for item in tuple(self._ui_responses):
            if item[:2] == (key, generation):
                self._ui_responses.pop(item, None)

    async def _emit(self, action: ConversationAction) -> None:
        if self._dispatch is None:
            raise RuntimeError("effect runner is not bound")
        await self._dispatch(action)

    def _spawn(self, awaitable: Awaitable[None], name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(awaitable, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()


def _text(effect: Effect, name: str) -> str:
    value = effect.payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"effect {name} is invalid")
    return value


def _integer(effect: Effect, name: str) -> int:
    value = effect.payload.get(name)
    if type(value) is not int or value < 0:
        raise ValueError(f"effect {name} is invalid")
    return value


def _selected_session(state) -> object | None:
    session_id = state.selected_session_id
    if session_id is None:
        return None
    pending = state.pending_sessions.get(session_id)
    if pending is not None:
        return pending
    if state.selected_session_path is None:
        return None
    return NativeSession(NativeSessionRef(session_id), None, 0, 0, None)


def _turn_content(items) -> TurnContent:
    text: list[str] = []
    images: list[str] = []
    for item in items:
        if item.kind in {"text", "voice"}:
            text.append(item.value)
        elif item.kind == "document":
            text.append(f"Inspect the attached document at {item.value}")
        elif item.kind == "photo":
            images.append(item.value)
    return TurnContent("\n\n".join(text) or "Inspect the attached input.", tuple(images))
