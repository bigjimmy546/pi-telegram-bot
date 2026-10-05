from __future__ import annotations

import unittest

from telegram_pi_bot.coordinator import transition
from tests.fakes import (
    add_photo,
    add_text,
    conversation_action,
    effect_kinds,
    empty_state,
    send_now,
    state_with_active_and_queued_turn,
    state_with_active_turn,
    steer_current,
    stop_action,
)


class CoordinatorTests(unittest.TestCase):
    def test_native_session_marker_is_the_validated_session_id(self):
        state = empty_state(chat_id=123456789)
        selected = transition(
            state,
            conversation_action(
                "use_session",
                session_id="native-1",
                native_path="native-1",
                now_ms=1,
            ),
        )
        self.assertEqual(selected.state.selected_session_path, "native-1")

    def test_native_session_markers_fail_closed(self):
        state = empty_state(chat_id=123456789)
        for native_path in ("../auth.json", "", "other-marker", None):
            result = transition(
                state,
                conversation_action(
                    "use_session",
                    session_id="native-1",
                    native_path=native_path,
                    now_ms=1,
                ),
            )
            self.assertIsNone(result.state.selected_session_path)
            self.assertIsNone(result.state.selected_session_id)

    def test_session_operation_owns_lease_and_releases_queued_input_once(self):
        state = transition(
            empty_state(now_ms=1_000),
            _action(
                "new_session",
                session_id="pending-operation",
                provider="ollama",
                model_id="qwen3.8-orcarouter:latest",
                thinking="medium",
            ),
        ).state
        operation = transition(
            state,
            _action(
                "begin_session_operation",
                operation_id="operation-1",
                operation_kind="configure",
                session_id="pending-operation",
                update_id=10,
                now_ms=2_000,
            ),
        )
        self.assertEqual(operation.state.active_turn.status, "configuring")
        self.assertEqual(effect_kinds(operation), ["run_session_operation"])

        queued = transition(
            operation.state,
            add_text("wait behind config", source_message_id=70, now_ms=3_000),
        )
        fired = transition(
            queued.state,
            _action(
                "bundle_timer_fired",
                bundle_id=queued.state.next_bundle.bundle_id,
                generation=queued.state.next_bundle.timer_generation,
                now_ms=8_000,
            ),
        )
        self.assertIsNone(fired.state.next_bundle.due_at_ms)
        self.assertNotIn("dispatch_turn", effect_kinds(fired))

        completed = transition(
            fired.state,
            _action(
                "session_operation_completed",
                turn_id="operation-1",
                success=True,
                provider="ollama",
                model_id="qwen3.8-orcarouter:latest",
                thinking="high",
                message="Session configuration updated.",
                now_ms=9_000,
            ),
        )
        self.assertEqual(effect_kinds(completed), ["dispatch_turn"])
        self.assertEqual(completed.state.active_turn.status, "dispatching")
        self.assertIsNone(completed.state.next_bundle)
        self.assertEqual(
            completed.state.pending_sessions["pending-operation"].thinking,
            "high",
        )

    def test_session_operation_is_refused_while_bundle_is_open(self):
        state = transition(
            empty_state(now_ms=1_000),
            _action(
                "new_session",
                session_id="pending-operation",
                provider="ollama",
                model_id="qwen3.8-orcarouter:latest",
                thinking="medium",
            ),
        ).state
        bundled = transition(
            state,
            add_text("already queued", source_message_id=71, now_ms=2_000),
        )

        refused = transition(
            bundled.state,
            _action(
                "begin_session_operation",
                operation_id="operation-2",
                operation_kind="configure",
                session_id="pending-operation",
                update_id=11,
                now_ms=3_000,
            ),
        )

        self.assertEqual(refused.state, bundled.state)
        self.assertEqual(effect_kinds(refused), [])
        self.assertIn("queued input", refused.replies[0])

    def test_text_bundles_for_five_seconds_and_media_for_ten(self):
        state = empty_state(now_ms=1_000)
        first = transition(state, add_text("one", source_message_id=10))
        self.assertEqual(first.state.bundle.due_at_ms, 6_000)

        second = transition(
            first.state,
            add_photo("photo-1", source_message_id=11, now_ms=2_000),
        )
        self.assertEqual(second.state.bundle.due_at_ms, 12_000)
        self.assertEqual(
            effect_kinds(second), ["cancel_timer", "schedule_timer", "react"]
        )

    def test_active_turn_queues_by_default_and_steer_is_explicit(self):
        queued = transition(
            state_with_active_turn("turn-1"),
            add_text("next", source_message_id=20),
        )
        self.assertIsNotNone(queued.state.next_bundle)
        self.assertNotIn("steer_turn", effect_kinds(queued))

        steered = transition(
            queued.state, steer_current(queued.state.next_bundle.bundle_id)
        )
        self.assertIn("steer_turn", effect_kinds(steered))
        accepted = transition(
            steered.state,
            _action(
                "steer_dispatched",
                bundle_id=steered.state.steering_bundle.bundle_id,
                accepted=True,
            ),
        )
        self.assertIsNone(accepted.state.next_bundle)

    def test_stop_freezes_local_queue_and_requests_native_abort_once(self):
        state = state_with_active_and_queued_turn()
        stopped = transition(state, stop_action())
        self.assertEqual(effect_kinds(stopped), ["abort_turn"])
        self.assertEqual(stopped.state.next_bundle.status, "frozen")

        repeated = transition(stopped.state, stop_action())
        self.assertNotIn("abort_turn", effect_kinds(repeated))

    def test_stop_during_reserved_dispatch_repeats_abort_after_late_acceptance(self):
        bundled = transition(
            empty_state(now_ms=1_000),
            add_text("stop this dispatch", source_message_id=21),
        )
        dispatching = transition(
            bundled.state,
            send_now(bundled.state.bundle.bundle_id, now_ms=2_000),
        )
        turn_id = dispatching.state.active_turn.turn_id
        stopped = transition(dispatching.state, stop_action(now_ms=2_001))
        self.assertEqual(effect_kinds(stopped), ["abort_turn"])

        with self.subTest(acknowledgement="bundle_dispatched"):
            accepted = transition(
                stopped.state,
                _action(
                    "bundle_dispatched",
                    bundle_id=stopped.state.bundle.bundle_id,
                    turn_id=turn_id,
                    accepted=True,
                    now_ms=2_002,
                ),
            )
            self.assertIn("abort_turn", effect_kinds(accepted))

        with self.subTest(acknowledgement="turn_accepted"):
            accepted = transition(
                stopped.state,
                _action(
                    "turn_accepted",
                    turn_id=turn_id,
                    session_id="unselected",
                    source_message_id=21,
                    now_ms=2_002,
                ),
            )
            self.assertIn("abort_turn", effect_kinds(accepted))

    def test_rejected_dispatch_freezes_content_notices_user_and_keeps_later_input_separate(self):
        bundled = transition(
            empty_state(now_ms=1_000),
            add_text("rejected content", source_message_id=22),
        )
        dispatching = transition(
            bundled.state,
            send_now(bundled.state.bundle.bundle_id, now_ms=2_000),
        )
        rejected = transition(
            dispatching.state,
            _action(
                "bundle_dispatched",
                bundle_id=dispatching.state.bundle.bundle_id,
                turn_id=dispatching.state.active_turn.turn_id,
                accepted=False,
                now_ms=2_001,
            ),
        )
        self.assertEqual(rejected.state.bundle.status, "frozen")
        reactions = [
            effect.payload.get("emoji")
            for effect in rejected.effects
            if effect.kind == "react"
        ]
        self.assertIn("😨", reactions)
        self.assertTrue(rejected.replies)
        self.assertTrue(
            any(
                "send" in reply.lower() or "retry" in reply.lower()
                for reply in rejected.replies
            )
        )

        later = transition(
            rejected.state,
            add_text("new input", source_message_id=23, now_ms=3_000),
        )
        self.assertNotEqual(
            later.state.bundle.bundle_id,
            rejected.state.bundle.bundle_id,
        )
        self.assertEqual(
            [item.value for item in later.state.bundle.items], ["new input"]
        )
        self.assertIn(
            "rejected content",
            [
                item.value
                for item in later.state.bundles[
                    rejected.state.bundle.bundle_id
                ].items
            ],
        )

    def test_unknown_unreserved_turn_acceptance_is_ignored(self):
        state = empty_state(now_ms=1_000)
        accepted = transition(
            state,
            _action(
                "turn_accepted",
                turn_id="unreserved-turn",
                session_id="native-1",
                source_message_id=24,
                now_ms=1_001,
            ),
        )
        self.assertEqual(accepted.state, state)
        self.assertEqual(effect_kinds(accepted), [])

    def test_recovery_marks_active_turn_uncertain_and_freezes_queue_without_dispatch(self):
        state = state_with_active_and_queued_turn()
        recovered = transition(state, _action("recover", now_ms=5_000))

        self.assertIsNone(recovered.state.active_turn)
        self.assertEqual(
            recovered.state.turns[state.active_turn.turn_id].status, "uncertain"
        )
        self.assertEqual(recovered.state.next_bundle.status, "frozen")
        self.assertNotIn("dispatch_turn", effect_kinds(recovered))
        self.assertIn(
            "😨",
            [
                effect.payload.get("emoji")
                for effect in recovered.effects
                if effect.kind == "react"
            ],
        )
        self.assertEqual(len(recovered.replies), 1)
        self.assertTrue(recovered.replies[0].strip())
        self.assertLessEqual(len(recovered.replies[0]), 300)

    def test_send_now_dispatches_bundle_and_stale_callback_is_ignored(self):
        bundled = transition(
            empty_state(now_ms=1_000), add_text("ready", source_message_id=31)
        )
        bundle_id = bundled.state.bundle.bundle_id
        sent = transition(bundled.state, send_now(bundle_id))
        self.assertIn("dispatch_turn", effect_kinds(sent))

        stale = transition(sent.state, send_now(bundle_id))
        self.assertEqual(effect_kinds(stale), [])

        second = transition(
            sent.state,
            add_text("later", source_message_id=32, now_ms=2_000),
        )
        attempted = transition(
            second.state,
            send_now(second.state.next_bundle.bundle_id, now_ms=2_000),
        )
        self.assertEqual(effect_kinds(attempted), [])
        self.assertEqual(attempted.state.next_bundle.status, "queued")
        self.assertEqual(attempted.state.active_turn.status, "dispatching")

    def test_matching_acceptance_consumes_reserved_dispatch_before_completion(self):
        bundled = transition(
            empty_state(now_ms=1_000),
            add_text("send once", source_message_id=33),
        )
        sent = transition(
            bundled.state,
            send_now(bundled.state.bundle.bundle_id, now_ms=2_000),
        )
        turn_id = sent.state.active_turn.turn_id

        accepted = transition(
            sent.state,
            _action(
                "turn_accepted",
                turn_id=turn_id,
                source_message_id=33,
                now_ms=2_001,
            ),
        )
        self.assertEqual(accepted.state.active_turn.status, "active")
        self.assertIsNone(accepted.state.bundle)

        completed = transition(
            accepted.state,
            _action(
                "turn_completed",
                turn_id=turn_id,
                status="completed",
                finished_at_ms=2_002,
                now_ms=2_002,
            ),
        )
        self.assertIsNone(completed.state.bundle)
        self.assertNotIn("send once", repr(completed.state))

    def test_matching_acceptance_preserves_early_same_turn_blocking_ui(self):
        bundled = transition(
            empty_state(now_ms=1_000),
            add_text("needs approval", source_message_id=35),
        )
        sent = transition(
            bundled.state,
            send_now(bundled.state.bundle.bundle_id, now_ms=2_000),
        )
        turn_id = sent.state.active_turn.turn_id
        opened = transition(
            sent.state,
            _action(
                "blocking_ui_opened",
                request_id="request-early",
                callback_key="callback-early",
                turn_id=turn_id,
                expires_at_ms=20_000,
                now_ms=2_001,
            ),
        )

        accepted = transition(
            opened.state,
            _action("turn_accepted", turn_id=turn_id, now_ms=2_002),
        )
        self.assertEqual(accepted.state.blocking_ui, opened.state.blocking_ui)
        self.assertEqual(
            accepted.state.callback_generation,
            opened.state.callback_generation,
        )
        answered = transition(
            accepted.state,
            _action(
                "answer_ui",
                callback_key="callback-early",
                generation=opened.state.blocking_ui.generation,
                response=True,
                now_ms=2_003,
            ),
        )
        self.assertEqual(effect_kinds(answered), ["answer_ui"])

    def test_successful_completion_before_dispatch_ack_consumes_prompt(self):
        bundled = transition(
            empty_state(now_ms=1_000),
            add_text("already ran", source_message_id=34),
        )
        sent = transition(
            bundled.state,
            send_now(bundled.state.bundle.bundle_id, now_ms=2_000),
        )
        completed = transition(
            sent.state,
            _action(
                "turn_completed",
                turn_id=sent.state.active_turn.turn_id,
                status="completed",
                finished_at_ms=2_001,
                now_ms=2_001,
            ),
        )

        self.assertIsNone(completed.state.bundle)
        self.assertTrue(
            completed.state.turns[sent.state.active_turn.turn_id].prompt_accepted
        )
        self.assertNotIn("already ran", repr(completed.state))

    def test_new_sessions_are_bot_local_and_use_selects_pending_record(self):
        state = empty_state(now_ms=1_000)
        first = transition(
            state,
            _action(
                "new_session",
                session_id="pending-1",
                name="Research",
                provider="ollama",
                model_id="qwen3.8-orcarouter:latest",
                thinking="medium",
            ),
        )
        second = transition(
            first.state,
            _action(
                "new_session",
                session_id="pending-2",
                name="Drafting",
                provider="antigravity",
                model_id="gemini-3.7-flash",
                thinking="high",
            ),
        )
        self.assertEqual(len(second.state.pending_sessions), 2)
        self.assertEqual(effect_kinds(first) + effect_kinds(second), [])

        selected = transition(
            second.state, _action("use_session", session_id="pending-1")
        )
        self.assertEqual(selected.state.selected_session_id, "pending-1")

    def test_configuration_survives_as_state_and_materialization_is_targeted(self):
        state = empty_state(now_ms=1_000)
        state = transition(
            state,
            _action(
                "new_session",
                session_id="pending-1",
                name="One",
                provider="ollama",
                model_id="qwen3.8-orcarouter:latest",
                thinking="medium",
            ),
        ).state
        state = transition(
            state,
            _action(
                "new_session",
                session_id="pending-2",
                name="Two",
                provider="ollama",
                model_id="qwen3.8-orcarouter:latest",
                thinking="low",
            ),
        ).state
        configured = transition(
            state,
            _action(
                "configure_session",
                session_id="pending-1",
                thinking="high",
            ),
        )
        self.assertEqual(configured.state.pending_sessions["pending-1"].thinking, "high")

        materialized = transition(
            configured.state,
            _action(
                "materialized_session",
                session_id="pending-1",
                native_session_id="pending-1",
                native_path="pending-1",
            ),
        )
        self.assertEqual(
            materialized.state.selected_session_path,
            "pending-1",
        )
        self.assertNotIn("pending-1", materialized.state.pending_sessions)
        self.assertIn("pending-2", materialized.state.pending_sessions)

    def test_recovery_materializes_only_an_exact_native_header_match(self):
        state = transition(
            empty_state(now_ms=1_000),
            _action(
                "new_session",
                session_id="pending-1",
                name="One",
                provider="ollama",
                model_id="qwen3.8-orcarouter:latest",
                thinking="medium",
            ),
        ).state
        missing = transition(
            state,
            _action(
                "recover_materialization",
                session_id="pending-1",
                native_session_id=None,
                native_path=None,
            ),
        )
        self.assertIn("pending-1", missing.state.pending_sessions)

        mismatch = transition(
            state,
            _action(
                "recover_materialization",
                session_id="pending-1",
                native_session_id="different-id",
                native_path=(
                    "/home/alice/.pi/agent/sessions/different.jsonl"
                ),
            ),
        )
        self.assertIn("pending-1", mismatch.state.pending_sessions)

        matched = transition(
            state,
            _action(
                "recover_materialization",
                session_id="pending-1",
                native_session_id="pending-1",
                native_path="pending-1",
            ),
        )
        self.assertNotIn("pending-1", matched.state.pending_sessions)
        self.assertEqual(
            matched.state.selected_session_path,
            "pending-1",
        )

    def test_blocking_ui_callback_requires_exact_key_generation_and_turn(self):
        state = state_with_active_turn("turn-1")
        opened = transition(
            state,
            _action(
                "blocking_ui_opened",
                request_id="request-1",
                callback_key="opaque-1",
                turn_id="turn-1",
                expires_at_ms=20_000,
                now_ms=1_000,
            ),
        )
        generation = opened.state.blocking_ui.generation
        stale = transition(
            opened.state,
            _action(
                "answer_ui",
                callback_key="opaque-1",
                generation=generation - 1,
                response=True,
                now_ms=2_000,
            ),
        )
        self.assertEqual(effect_kinds(stale), [])
        self.assertIsNotNone(stale.state.blocking_ui)

        answered = transition(
            opened.state,
            _action(
                "answer_ui",
                callback_key="opaque-1",
                generation=generation,
                response=True,
                now_ms=2_000,
            ),
        )
        self.assertEqual(effect_kinds(answered), ["answer_ui"])
        self.assertIsNone(answered.state.blocking_ui)

        timed_out = transition(
            opened.state,
            _action(
                "blocking_ui_timeout",
                callback_key="opaque-1",
                generation=generation,
                now_ms=20_000,
            ),
        )
        self.assertEqual(effect_kinds(timed_out), ["cancel_ui"])
        self.assertIsNone(timed_out.state.blocking_ui)

    def test_turn_completion_starts_due_queued_bundle_once(self):
        queued = transition(
            state_with_active_turn("turn-1"),
            add_text("next", source_message_id=40),
        )
        due = transition(
            queued.state,
            _action(
                "bundle_timer_fired",
                bundle_id=queued.state.next_bundle.bundle_id,
                generation=queued.state.next_bundle.timer_generation,
                now_ms=queued.state.next_bundle.due_at_ms,
            ),
        )
        completed = transition(
            due.state,
            _action(
                "turn_completed",
                turn_id="turn-1",
                status="completed",
                finished_at_ms=7_000,
                now_ms=7_000,
            ),
        )
        self.assertEqual(effect_kinds(completed), ["react", "dispatch_turn"])
        self.assertEqual(completed.state.bundle.status, "dispatching")
        self.assertIsNone(completed.state.next_bundle)

        duplicate = transition(
            completed.state,
            _action(
                "turn_completed",
                turn_id="turn-1",
                status="completed",
                finished_at_ms=7_000,
                now_ms=7_000,
            ),
        )
        self.assertEqual(effect_kinds(duplicate), [])

    def test_stopped_bundle_requires_explicit_send_or_cancel(self):
        stopped = transition(state_with_active_and_queued_turn(), stop_action())
        completed = transition(
            stopped.state,
            _action(
                "turn_completed",
                turn_id="turn-1",
                status="aborted",
                finished_at_ms=2_000,
                now_ms=2_000,
            ),
        )
        self.assertEqual(completed.state.next_bundle.status, "frozen")
        self.assertNotIn("dispatch_turn", effect_kinds(completed))

        sent = transition(
            completed.state,
            send_now(completed.state.next_bundle.bundle_id, now_ms=3_000),
        )
        self.assertIn("dispatch_turn", effect_kinds(sent))

        cancelled = transition(
            completed.state,
            _action(
                "cancel_bundle",
                bundle_id=completed.state.next_bundle.bundle_id,
                now_ms=3_000,
            ),
        )
        self.assertIsNone(cancelled.state.next_bundle)
        self.assertEqual(effect_kinds(cancelled), ["cancel_timer"])

        extended = transition(
            completed.state,
            add_text("also frozen", source_message_id=41, now_ms=3_000),
        )
        self.assertEqual(extended.state.next_bundle.status, "frozen")
        self.assertEqual(len(extended.state.next_bundle.items), 1)
        self.assertEqual(extended.state.bundle.items[0].value, "also frozen")

        prioritized = transition(
            extended.state,
            send_now(extended.state.next_bundle.bundle_id, now_ms=4_000),
        )
        self.assertEqual(prioritized.state.bundle.items[0].value, "queued")
        self.assertEqual(prioritized.state.next_bundle.status, "open")
        self.assertEqual(
            prioritized.state.next_bundle.items[0].value,
            "also frozen",
        )

    def test_sending_frozen_bundle_preserves_newer_bundle_deadline(self):
        stopped = transition(state_with_active_and_queued_turn(), stop_action())
        idle = transition(
            stopped.state,
            _action(
                "turn_completed",
                turn_id="turn-1",
                status="aborted",
                finished_at_ms=2_000,
                now_ms=2_000,
            ),
        )
        newer = transition(
            idle.state,
            add_text("wait for me", source_message_id=44, now_ms=3_000),
        )
        frozen_id = newer.state.next_bundle.bundle_id
        newer_id = newer.state.bundle.bundle_id
        generation = newer.state.bundle.timer_generation

        sent = transition(newer.state, send_now(frozen_id, now_ms=4_000))
        self.assertEqual(sent.state.next_bundle.bundle_id, newer_id)
        self.assertEqual(sent.state.next_bundle.status, "open")
        self.assertEqual(sent.state.next_bundle.due_at_ms, 8_000)
        self.assertNotIn(
            newer_id,
            [
                effect.payload.get("bundle_id")
                for effect in sent.effects
                if effect.kind == "cancel_timer"
            ],
        )

        completed = transition(
            sent.state,
            _action(
                "turn_completed",
                turn_id=sent.state.active_turn.turn_id,
                status="completed",
                finished_at_ms=4_002,
                now_ms=4_002,
            ),
        )
        self.assertNotIn("dispatch_turn", effect_kinds(completed))
        self.assertEqual(completed.state.bundle.bundle_id, newer_id)
        early = transition(
            completed.state,
            _action(
                "bundle_timer_fired",
                bundle_id=newer_id,
                generation=generation,
                now_ms=7_999,
            ),
        )
        self.assertNotIn("dispatch_turn", effect_kinds(early))
        due = transition(
            early.state,
            _action(
                "bundle_timer_fired",
                bundle_id=newer_id,
                generation=generation,
                now_ms=8_000,
            ),
        )
        self.assertIn("dispatch_turn", effect_kinds(due))

    def test_in_flight_steer_keeps_its_identity_when_more_input_arrives(self):
        queued = transition(
            state_with_active_turn("turn-1"),
            add_text("steer me", source_message_id=42),
        )
        steered = transition(
            queued.state,
            steer_current(queued.state.next_bundle.bundle_id),
        )
        steering_id = steered.state.steering_bundle.bundle_id
        later = transition(
            steered.state,
            add_text("next turn", source_message_id=43, now_ms=2_000),
        )
        self.assertEqual(later.state.steering_bundle.bundle_id, steering_id)
        self.assertEqual(later.state.next_bundle.items[0].value, "next turn")

        accepted = transition(
            later.state,
            _action(
                "steer_dispatched",
                bundle_id=steering_id,
                accepted=True,
                now_ms=2_000,
            ),
        )
        self.assertIsNone(accepted.state.steering_bundle)
        self.assertEqual(accepted.state.next_bundle.items[0].value, "next turn")
        self.assertNotIn(steering_id, accepted.state.bundles)

    def test_uncertain_turn_identity_is_terminal(self):
        uncertain = transition(
            state_with_active_turn("turn-uncertain"),
            _action(
                "turn_completed",
                turn_id="turn-uncertain",
                status="uncertain",
                finished_at_ms=2_000,
                now_ms=2_000,
            ),
        )
        late_acceptance = transition(
            uncertain.state,
            _action(
                "turn_accepted",
                turn_id="turn-uncertain",
                session_id="native-1",
                source_message_id=1,
                now_ms=3_000,
            ),
        )
        self.assertEqual(
            late_acceptance.state.turns["turn-uncertain"].status,
            "uncertain",
        )
        late_completion = transition(
            late_acceptance.state,
            _action(
                "turn_completed",
                turn_id="turn-uncertain",
                status="completed",
                finished_at_ms=3_000,
                now_ms=3_000,
            ),
        )
        self.assertEqual(
            late_completion.state.turns["turn-uncertain"].status,
            "uncertain",
        )

    def test_native_session_selection_and_malformed_actions_fail_closed(self):
        native = transition(
            empty_state(now_ms=1_000),
            _action(
                "use_session",
                session_id="native-1",
                native_path="native-1",
            ),
        )
        self.assertEqual(native.state.selected_session_id, "native-1")
        traversal = transition(
            native.state,
            _action(
                "use_session",
                session_id="native-1",
                native_path="../auth.json",
            ),
        )
        self.assertEqual(traversal.state, native.state)

        bundled = transition(
            empty_state(now_ms=1_000), add_text("ready", source_message_id=50)
        )
        missing_turn = transition(
            bundled.state,
            _action(
                "bundle_dispatched",
                bundle_id=bundled.state.bundle.bundle_id,
                accepted=True,
            ),
        )
        self.assertEqual(missing_turn.state, bundled.state)

        active = state_with_active_turn("turn-1", now_ms=1_000)
        bad_ui = transition(
            active,
            _action(
                "blocking_ui_opened",
                request_id="request-1",
                callback_key="opaque-1",
                turn_id="turn-1",
                expires_at_ms="later",
            ),
        )
        self.assertEqual(bad_ui.state, active)

        bad_artifact = transition(
            empty_state(),
            _action(
                "artifact_queued",
                artifact_id="artifact-1",
                staging_path="/home/alice/report.pdf",
                sha256="bad",
                size=-1,
            ),
        )
        self.assertEqual(bad_artifact.state, empty_state())

    def test_state_and_action_repr_do_not_expose_pending_prompt_text(self):
        action = add_text("PRIVATE-PENDING-BODY", source_message_id=60)
        bundled = transition(empty_state(), action)
        self.assertNotIn("PRIVATE-PENDING-BODY", repr(action))
        self.assertNotIn("PRIVATE-PENDING-BODY", repr(bundled.state))


def _action(kind: str, **values):
    from telegram_pi_bot.model import ConversationAction

    return ConversationAction(kind=kind, **values)


if __name__ == "__main__":
    unittest.main()
