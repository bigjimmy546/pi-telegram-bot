from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telegram_pi_bot.coordinator import transition
from telegram_pi_bot.store import StoreConflict
from tests.fakes import (
    add_text,
    empty_state,
    make_store,
    send_now,
    state_with_active_turn,
    steer_current,
)


class ControlStoreTests(unittest.TestCase):
    def test_recovery_releases_claimed_session_operation_as_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = make_store(root)
            state = empty_state(chat_id=120, now_ms=1_000)
            created = transition(
                state,
                _action(
                    "new_session",
                    session_id="pending-operation",
                    provider="ollama",
                    model_id="qwen3.8-orcarouter:latest",
                    thinking="medium",
                    now_ms=1_000,
                ),
            )
            store.commit(state.version, created)
            state = store.load(120)
            started = transition(
                state,
                _action(
                    "begin_session_operation",
                    operation_id="operation-restart",
                    operation_kind="configure",
                    session_id="pending-operation",
                    update_id=50,
                    now_ms=2_000,
                ),
            )
            store.commit(state.version, started)
            effect = next(
                item
                for item in store.pending_effects(120)
                if item.kind == "run_session_operation"
            )
            self.assertTrue(store.claim_effect(effect.effect_id))

            reopened = make_store(root)
            recovery = reopened.recover(now_ms=3_000)
            state = reopened.load(120)

            self.assertIsNone(state.active_turn)
            self.assertEqual(state.turns["operation-restart"].status, "uncertain")
            self.assertIn("session operation", recovery.replies[0])

    def test_compare_and_swap_rejects_stale_version_without_duplicate_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            initial = empty_state(chat_id=101, now_ms=1_000)
            change = transition(initial, add_text("queued text", source_message_id=4))
            store.commit(initial.version, change)
            with self.assertRaises(StoreConflict):
                store.commit(initial.version, change)
            self.assertEqual(store.load(101).version, change.state.version)

    def test_action_and_effect_ids_are_idempotent_across_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = make_store(root)
            initial = empty_state(chat_id=102, now_ms=1_000)
            action = _action(
                "add_text",
                action_id="telegram-update-77",
                text="once",
                source_message_id=77,
                now_ms=1_000,
            )
            result = transition(initial, action)
            store.commit(initial.version, result)
            effect_ids = tuple(effect.effect_id for effect in store.pending_effects(102))
            self.assertEqual(len(effect_ids), len(set(effect_ids)))

            reopened = make_store(root)
            before = reopened.load(102)
            with self.assertRaises(StoreConflict):
                reopened.commit(initial.version, result)
            after = reopened.load(102)
            self.assertEqual(after.version, before.version)
            self.assertEqual(after.bundle.bundle_id, before.bundle.bundle_id)
            self.assertEqual(
                tuple(effect.effect_id for effect in reopened.pending_effects(102)),
                effect_ids,
            )

    def test_completed_message_replay_cannot_start_another_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            state = empty_state(chat_id=118, now_ms=1_000)
            inbound = add_text("message ten", source_message_id=10, now_ms=1_000)
            first_input = transition(state, inbound)
            original_action_id = first_input.action_id
            store.commit(state.version, first_input)

            state = store.load(118)
            dispatch = transition(
                state,
                send_now(state.bundle.bundle_id, now_ms=2_000),
            )
            store.commit(state.version, dispatch)
            state = store.load(118)
            dispatch_effect = next(
                effect
                for effect in store.pending_effects(118)
                if effect.kind == "dispatch_turn"
            )
            self.assertTrue(store.claim_effect(dispatch_effect.effect_id))

            accepted = transition(
                state,
                _action(
                    "bundle_dispatched",
                    bundle_id=state.bundle.bundle_id,
                    turn_id=state.active_turn.turn_id,
                    accepted=True,
                    caused_by_effect_id=dispatch_effect.effect_id,
                    now_ms=3_000,
                ),
            )
            store.commit(state.version, accepted)
            state = store.load(118)
            turn_id = state.active_turn.turn_id
            completed = transition(
                state,
                _action(
                    "turn_completed",
                    turn_id=turn_id,
                    status="completed",
                    finished_at_ms=4_000,
                    now_ms=4_000,
                ),
            )
            store.commit(state.version, completed)
            self.assertEqual(store.load(118).turns[turn_id].status, "completed")

            replay = transition(store.load(118), inbound)
            self.assertEqual(replay.action_id, original_action_id)
            with self.assertRaises(StoreConflict):
                store.commit(store.load(118).version, replay)

            persisted = store.load(118)
            self.assertEqual(persisted.turns[turn_id].status, "completed")
            self.assertIsNone(persisted.active_turn)
            self.assertIsNone(persisted.bundle)
            self.assertNotIn(
                "dispatch_turn",
                [effect.kind for effect in store.pending_effects(118)],
            )

    def test_rejected_turn_completion_dispatches_queued_bundle_and_keeps_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            store, state, first_bundle_id, queued_bundle_id, first_effect = (
                _persist_dispatching_with_queued_bundle(Path(directory), chat_id=120)
            )
            rejected = transition(
                state,
                _action(
                    "turn_completed",
                    turn_id=state.active_turn.turn_id,
                    status="rejected",
                    finished_at_ms=8_000,
                    now_ms=8_000,
                    caused_by_effect_id=first_effect.effect_id,
                ),
            )
            new_dispatches = [
                effect
                for effect in rejected.effects
                if effect.kind == "dispatch_turn"
            ]
            self.assertEqual(len(new_dispatches), 1)
            self.assertEqual(new_dispatches[0].payload["bundle_id"], queued_bundle_id)
            self.assertEqual(
                rejected.state.active_turn.turn_id,
                new_dispatches[0].payload["turn_id"],
            )
            self.assertEqual(rejected.state.active_turn.source_message_id, 11)
            self.assertEqual(rejected.state.active_turn.status, "dispatching")
            self.assertEqual(rejected.state.bundle.bundle_id, queued_bundle_id)
            self.assertEqual(rejected.state.bundle.status, "dispatching")
            self.assertEqual(rejected.state.bundles[first_bundle_id].status, "frozen")
            store.commit(state.version, rejected)

            persisted = store.load(120)
            pending_dispatches = [
                effect
                for effect in store.pending_effects(120)
                if effect.kind == "dispatch_turn"
            ]
            self.assertEqual(len(pending_dispatches), 1)
            self.assertEqual(pending_dispatches[0].payload["bundle_id"], queued_bundle_id)
            self._assert_frozen_bundle_remains_addressable(
                store, persisted, first_bundle_id
            )

    def test_rejected_dispatch_ack_dispatches_queued_bundle_and_keeps_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            store, state, first_bundle_id, queued_bundle_id, first_effect = (
                _persist_dispatching_with_queued_bundle(Path(directory), chat_id=121)
            )
            rejected = transition(
                state,
                _action(
                    "bundle_dispatched",
                    bundle_id=first_bundle_id,
                    turn_id=state.active_turn.turn_id,
                    accepted=False,
                    now_ms=8_000,
                    caused_by_effect_id=first_effect.effect_id,
                ),
            )
            new_dispatches = [
                effect
                for effect in rejected.effects
                if effect.kind == "dispatch_turn"
            ]
            self.assertEqual(len(new_dispatches), 1)
            self.assertEqual(new_dispatches[0].payload["bundle_id"], queued_bundle_id)
            self.assertEqual(
                rejected.state.active_turn.turn_id,
                new_dispatches[0].payload["turn_id"],
            )
            self.assertEqual(rejected.state.active_turn.source_message_id, 11)
            self.assertEqual(rejected.state.active_turn.status, "dispatching")
            self.assertEqual(rejected.state.bundle.bundle_id, queued_bundle_id)
            self.assertEqual(rejected.state.bundle.status, "dispatching")
            self.assertEqual(rejected.state.bundles[first_bundle_id].status, "frozen")
            store.commit(state.version, rejected)

            persisted = store.load(121)
            pending_dispatches = [
                effect
                for effect in store.pending_effects(121)
                if effect.kind == "dispatch_turn"
            ]
            self.assertEqual(len(pending_dispatches), 1)
            self.assertEqual(pending_dispatches[0].payload["bundle_id"], queued_bundle_id)
            self._assert_frozen_bundle_remains_addressable(
                store, persisted, first_bundle_id
            )

    def _assert_frozen_bundle_remains_addressable(
        self, store, state, bundle_id: str
    ) -> None:
        self.assertEqual(state.bundles[bundle_id].status, "frozen")
        send = transition(
            state,
            _action("send_now", bundle_id=bundle_id, now_ms=8_100),
        )
        self.assertNotIn("dispatch_turn", [effect.kind for effect in send.effects])
        self.assertEqual(send.state, state)

        cancelled = transition(
            state,
            _action("cancel_bundle", bundle_id=bundle_id, now_ms=8_101),
        )
        self.assertNotIn(bundle_id, cancelled.state.bundles)
        store.commit(state.version, cancelled)
        self.assertNotIn(bundle_id, store.load(state.chat_id).bundles)

    def test_rejected_turn_can_retry_and_commit_its_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            state = empty_state(chat_id=119, now_ms=1_000)
            inbound = transition(
                state,
                add_text("retry me", source_message_id=10, now_ms=1_000),
            )
            store.commit(state.version, inbound)

            state = store.load(119)
            first_dispatch = transition(
                state,
                send_now(state.bundle.bundle_id, now_ms=2_000),
            )
            store.commit(state.version, first_dispatch)
            state = store.load(119)
            first_effect = next(
                effect
                for effect in store.pending_effects(119)
                if effect.kind == "dispatch_turn"
            )
            self.assertTrue(store.claim_effect(first_effect.effect_id))
            rejected = transition(
                state,
                _action(
                    "turn_completed",
                    turn_id=state.active_turn.turn_id,
                    source_message_id=10,
                    status="rejected",
                    finished_at_ms=3_000,
                    now_ms=3_000,
                    caused_by_effect_id=first_effect.effect_id,
                ),
            )
            store.commit(state.version, rejected)
            self.assertEqual(rejected.state.bundle.status, "frozen")
            self.assertIsNone(rejected.state.bundle.due_at_ms)
            self.assertEqual(len(rejected.replies), 1)
            self.assertIn(
                "😨",
                [
                    effect.payload.get("emoji")
                    for effect in rejected.effects
                    if effect.kind == "react"
                ],
            )

            state = store.load(119)
            retry_dispatch = transition(
                state,
                send_now(state.bundle.bundle_id, now_ms=4_000),
            )
            store.commit(state.version, retry_dispatch)
            state = store.load(119)
            retry_effect = next(
                effect
                for effect in store.pending_effects(119)
                if effect.kind == "dispatch_turn"
            )
            self.assertTrue(store.claim_effect(retry_effect.effect_id))
            accepted = transition(
                state,
                _action(
                    "bundle_dispatched",
                    bundle_id=state.bundle.bundle_id,
                    turn_id=state.active_turn.turn_id,
                    accepted=True,
                    caused_by_effect_id=retry_effect.effect_id,
                    now_ms=5_000,
                ),
            )
            store.commit(state.version, accepted)
            state = store.load(119)
            retry_turn_id = state.active_turn.turn_id
            completed = transition(
                state,
                _action(
                    "turn_completed",
                    turn_id=retry_turn_id,
                    source_message_id=10,
                    status="completed",
                    finished_at_ms=6_000,
                    now_ms=6_000,
                ),
            )
            self.assertNotEqual(rejected.action_id, completed.action_id)
            store.commit(state.version, completed)

            persisted = store.load(119)
            self.assertIsNone(persisted.active_turn)
            self.assertEqual(persisted.turns[retry_turn_id].status, "completed")

    def test_effects_keep_commit_order_when_timestamps_and_ids_sort_differently(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            state = empty_state(chat_id=109, now_ms=1_000)
            first = transition(
                state,
                add_text("nine", source_message_id=9, now_ms=1_000),
            )
            store.commit(state.version, first)
            state = store.load(109)
            second = transition(
                state,
                add_text("ten", source_message_id=10, now_ms=1_000),
            )
            store.commit(state.version, second)

            effects = store.pending_effects(109)
            self.assertEqual(
                [effect.kind for effect in effects],
                [
                    "schedule_timer",
                    "react",
                    "cancel_timer",
                    "schedule_timer",
                    "react",
                ],
            )
            self.assertEqual(effects[-2].payload["generation"], 2)

    def test_blocking_ui_actions_at_same_time_have_distinct_ids_and_persist_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            state = empty_state(chat_id=115, now_ms=1_000)
            state, turn_id = _persist_active_turn(
                store,
                state,
                "ui owner",
                source_message_id=115,
            )

            first = transition(
                state,
                _action(
                    "blocking_ui_opened",
                    request_id="request-first",
                    callback_key="callback-first",
                    turn_id=turn_id,
                    expires_at_ms=20_000,
                    now_ms=2_000,
                ),
            )
            store.commit(state.version, first)
            state = store.load(115)
            second = transition(
                state,
                _action(
                    "blocking_ui_opened",
                    request_id="request-second",
                    callback_key="callback-second",
                    turn_id=turn_id,
                    expires_at_ms=20_000,
                    now_ms=2_000,
                ),
            )

            self.assertEqual(
                [effect.kind for effect in second.effects], ["cancel_ui"]
            )
            store.commit(state.version, second)
            self.assertNotEqual(first.action_id, second.action_id)
            persisted = store.pending_effects(115)
            self.assertEqual(
                [
                    effect.payload["request_id"]
                    for effect in persisted
                    if effect.kind == "cancel_ui"
                ],
                ["request-second"],
            )

    def test_legacy_absolute_marker_loads_and_new_marker_persists(self):
        from dataclasses import replace as dataclass_replace

        from telegram_pi_bot.model import Transition

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = make_store(root)
            state = empty_state(chat_id=104, now_ms=1_000)
            legacy_state = dataclass_replace(
                state,
                version=1,
                selected_session_id="native-legacy",
                selected_session_path=(
                    "/home/alice/.pi/agent/sessions/"
                    "--home-alice--/native-legacy.jsonl"
                ),
            )
            store.commit(0, Transition(legacy_state, ()))

            reopened = make_store(root)
            persisted = reopened.load(104)
            self.assertEqual(persisted.selected_session_id, "native-legacy")
            self.assertEqual(
                persisted.selected_session_path,
                "/home/alice/.pi/agent/sessions/--home-alice--/native-legacy.jsonl",
            )
            changed = transition(
                persisted,
                _action(
                    "use_session",
                    session_id="native-new",
                    native_path="native-new",
                ),
            )
            reopened.commit(persisted.version, changed)
            self.assertEqual(reopened.load(104).selected_session_path, "native-new")

    def test_restart_preserves_pending_session_config_and_materialization_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = make_store(root)
            state = empty_state(chat_id=103, now_ms=1_000)
            created = transition(
                state,
                _action(
                    "new_session",
                    session_id="pending-a",
                    name="Research",
                    provider="ollama",
                    model_id="qwen3.8-orcarouter:latest",
                    thinking="medium",
                ),
            )
            store.commit(state.version, created)
            state = store.load(103)
            changed = transition(
                state,
                _action(
                    "configure_session",
                    session_id="pending-a",
                    thinking="high",
                ),
            )
            store.commit(state.version, changed)

            reopened = make_store(root)
            persisted = reopened.load(103)
            self.assertEqual(persisted.pending_sessions["pending-a"].thinking, "high")
            materialized = transition(
                persisted,
                _action(
                    "materialized_session",
                    session_id="pending-a",
                    native_session_id="pending-a",
                    native_path="pending-a",
                ),
            )
            reopened.commit(persisted.version, materialized)
            self.assertEqual(
                reopened.load(103).selected_session_path,
                "pending-a",
            )

    def test_recovery_marks_accepted_turn_uncertain_and_cancels_orphan_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            initial = empty_state(chat_id=104, now_ms=1_000)
            accepted, turn_id = _persist_active_turn(
                store,
                initial,
                "private prompt body",
                source_message_id=104,
            )
            opened = transition(
                accepted,
                _action(
                    "blocking_ui_opened",
                    request_id="ui-1",
                    callback_key="callback-ui-1",
                    turn_id=accepted.active_turn.turn_id,
                    expires_at_ms=20_000,
                    now_ms=1_010,
                ),
            )
            store.commit(accepted.version, opened)

            reopened = make_store(Path(directory))
            recovery = reopened.recover(now_ms=2_000)
            state = reopened.load(104)
            self.assertIsNone(state.active_turn)
            self.assertEqual(state.turns[turn_id].status, "uncertain")
            self.assertIsNone(state.blocking_ui)
            self.assertIn("cancel_ui", [effect.kind for effect in recovery.effects])
            self.assertNotIn("private prompt body", repr(state))
            self.assertEqual(reopened.recover(now_ms=3_000).effects, ())

    def test_recovery_exposes_notice_and_freezes_queued_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            state = empty_state(chat_id=116, now_ms=1_000)
            state, turn_id = _persist_active_turn(
                store,
                state,
                "unsafe turn",
                source_message_id=116,
            )
            queued = transition(
                state, add_text("queued after restart", source_message_id=117)
            )
            store.commit(state.version, queued)

            recovered = store.recover(now_ms=3_000)
            persisted = store.load(116)
            self.assertIsNone(persisted.active_turn)
            self.assertEqual(persisted.turns[turn_id].status, "uncertain")
            self.assertEqual(persisted.next_bundle.status, "frozen")
            self.assertNotIn(
                "dispatch_turn", [effect.kind for effect in recovered.effects]
            )
            self.assertEqual(len(recovered.replies), 1)
            self.assertTrue(recovered.replies[0].strip())
            self.assertLessEqual(len(recovered.replies[0]), 300)

    def test_recovery_preserves_pending_bundle_and_rebuilds_its_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = make_store(root)
            initial = empty_state(chat_id=108, now_ms=1_000)
            bundled = transition(
                initial,
                add_text("pending after restart", source_message_id=8, now_ms=1_000),
            )
            store.commit(initial.version, bundled)

            reopened = make_store(root)
            recovery = reopened.recover(now_ms=2_000)
            restored = reopened.load(108)
            self.assertEqual(
                restored.bundle.items[0].value,
                "pending after restart",
            )
            timers = [
                effect
                for effect in recovery.effects
                if effect.kind == "schedule_timer"
            ]
            self.assertEqual(len(timers), 1)
            self.assertEqual(
                timers[0].payload["generation"],
                restored.bundle.timer_generation,
            )

    def test_claimed_dispatch_becomes_uncertain_and_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            state = empty_state(chat_id=110, now_ms=1_000)
            bundled = transition(
                state,
                add_text("may have run", source_message_id=10, now_ms=1_000),
            )
            store.commit(state.version, bundled)
            state = store.load(110)
            dispatch = transition(
                state,
                _action(
                    "send_now",
                    bundle_id=state.bundle.bundle_id,
                    now_ms=2_000,
                ),
            )
            store.commit(state.version, dispatch)
            dispatch_effect = next(
                effect
                for effect in store.pending_effects(110)
                if effect.kind == "dispatch_turn"
            )
            self.assertTrue(store.claim_effect(dispatch_effect.effect_id))

            store.recover(now_ms=3_000)
            recovered = store.load(110)
            self.assertIsNone(recovered.active_turn)
            self.assertEqual(
                recovered.turns[dispatch.state.active_turn.turn_id].status,
                "uncertain",
            )
            self.assertIsNone(recovered.bundle)
            self.assertNotIn(
                dispatch_effect.effect_id,
                {effect.effect_id for effect in store.pending_effects(110)},
            )
            self.assertNotIn("may have run", repr(recovered))

    def test_prune_retires_expired_unattempted_dispatch_and_releases_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            state = empty_state(chat_id=113, now_ms=1_000)
            bundled = transition(
                state,
                add_text("never attempted", source_message_id=14, now_ms=1_000),
            )
            store.commit(state.version, bundled)
            state = store.load(113)
            dispatched = transition(
                state,
                _action(
                    "send_now",
                    bundle_id=state.bundle.bundle_id,
                    now_ms=2_000,
                ),
            )
            store.commit(state.version, dispatched)
            dispatch_effect = next(
                effect
                for effect in store.pending_effects(113)
                if effect.kind == "dispatch_turn"
            )

            store.prune(1_000 + 24 * 60 * 60 * 1_000)
            recovered = store.load(113)
            self.assertIsNone(recovered.active_turn)
            self.assertIsNone(recovered.bundle)
            self.assertEqual(
                recovered.turns[dispatched.state.active_turn.turn_id].status,
                "rejected",
            )
            self.assertNotIn(
                dispatch_effect.effect_id,
                {effect.effect_id for effect in store.pending_effects(113)},
            )

            fresh = transition(
                recovered,
                add_text("fresh", source_message_id=15, now_ms=recovered.now_ms + 1),
            )
            sent = transition(
                fresh.state,
                _action(
                    "send_now",
                    bundle_id=fresh.state.bundle.bundle_id,
                    now_ms=fresh.state.now_ms + 1,
                ),
            )
            self.assertIn("dispatch_turn", [effect.kind for effect in sent.effects])

    def test_dispatch_acceptance_atomically_settles_claimed_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            state = empty_state(chat_id=111, now_ms=1_000)
            bundled = transition(
                state,
                add_text("accepted once", source_message_id=11, now_ms=1_000),
            )
            store.commit(state.version, bundled)
            state = store.load(111)
            dispatch = transition(
                state,
                _action(
                    "send_now",
                    bundle_id=state.bundle.bundle_id,
                    now_ms=2_000,
                ),
            )
            store.commit(state.version, dispatch)
            state = store.load(111)
            dispatch_effect = next(
                effect
                for effect in store.pending_effects(111)
                if effect.kind == "dispatch_turn"
            )
            self.assertTrue(store.claim_effect(dispatch_effect.effect_id))
            accepted = transition(
                state,
                _action(
                    "bundle_dispatched",
                    bundle_id=state.bundle.bundle_id,
                    turn_id=state.active_turn.turn_id,
                    accepted=True,
                    caused_by_effect_id=dispatch_effect.effect_id,
                    now_ms=3_000,
                ),
            )
            store.commit(state.version, accepted)
            self.assertEqual(store.load(111).active_turn.status, "active")
            self.assertNotIn(
                dispatch_effect.effect_id,
                {effect.effect_id for effect in store.pending_effects(111)},
            )

    def test_accepted_steer_is_removed_without_losing_later_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = make_store(root)
            state = state_with_active_turn(
                "turn-steer",
                chat_id=112,
                now_ms=1_000,
            )
            queued = transition(
                state,
                add_text("accepted steer body", source_message_id=12, now_ms=1_000),
            )
            store.commit(state.version, queued)
            state = store.load(112)
            steered = transition(
                state,
                steer_current(state.next_bundle.bundle_id, now_ms=2_000),
            )
            store.commit(state.version, steered)
            state = store.load(112)
            steering_id = state.steering_bundle.bundle_id
            later = transition(
                state,
                add_text("later body", source_message_id=13, now_ms=3_000),
            )
            store.commit(state.version, later)
            state = store.load(112)
            steer_effect = next(
                effect
                for effect in store.pending_effects(112)
                if effect.kind == "steer_turn"
            )
            self.assertTrue(store.claim_effect(steer_effect.effect_id))
            accepted = transition(
                state,
                _action(
                    "steer_dispatched",
                    bundle_id=steering_id,
                    accepted=True,
                    caused_by_effect_id=steer_effect.effect_id,
                    now_ms=4_000,
                ),
            )
            store.commit(state.version, accepted)

            restored = store.load(112)
            self.assertIsNone(restored.steering_bundle)
            self.assertEqual(restored.next_bundle.items[0].value, "later body")
            self.assertNotIn(steering_id, restored.bundles)
            self.assertNotIn(b"accepted steer body", (root / "control.sqlite3").read_bytes())

    def test_terminal_turn_retains_metadata_but_not_prompt_or_response_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            initial = empty_state(chat_id=105, now_ms=1_000)
            finished = transition(
                initial,
                _action(
                    "turn_completed",
                    turn_id="turn-2",
                    session_id="native-2",
                    prompt="UNIQUE-PRIVATE-PROMPT",
                    response="UNIQUE-PRIVATE-RESPONSE",
                    started_at_ms=1_000,
                    finished_at_ms=2_000,
                    status="completed",
                ),
            )
            store.commit(initial.version, finished)
            loaded = store.load(105)
            self.assertNotIn("UNIQUE-PRIVATE-PROMPT", repr(loaded))
            self.assertNotIn("UNIQUE-PRIVATE-RESPONSE", repr(loaded))
            self.assertEqual(loaded.turns["turn-2"].status, "completed")
            database_bytes = (Path(directory) / "control.sqlite3").read_bytes()
            self.assertNotIn(b"UNIQUE-PRIVATE-PROMPT", database_bytes)
            self.assertNotIn(b"UNIQUE-PRIVATE-RESPONSE", database_bytes)

    def test_terminal_prompt_content_is_deleted_and_expiry_windows_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            store = make_store(Path(directory))
            initial = empty_state(chat_id=106, now_ms=1_000)
            queued = transition(
                initial, add_text("pending body", source_message_id=6, now_ms=1_000)
            )
            store.commit(initial.version, queued)
            state = store.load(106)
            dispatch = transition(
                state,
                _action(
                    "send_now",
                    bundle_id=state.bundle.bundle_id,
                    now_ms=2_000,
                ),
            )
            store.commit(state.version, dispatch)
            state = store.load(106)
            dispatch_effect = next(
                effect
                for effect in store.pending_effects(106)
                if effect.kind == "dispatch_turn"
            )
            completed = transition(
                state,
                _action(
                    "bundle_dispatched",
                    bundle_id=state.bundle.bundle_id,
                    turn_id=state.active_turn.turn_id,
                    accepted=True,
                    prompt="pending body",
                    now_ms=2_000,
                    caused_by_effect_id=dispatch_effect.effect_id,
                ),
            )
            store.commit(state.version, completed)
            self.assertNotIn("pending body", repr(store.load(106)))

            # Bundle/artifact staging expires after one day; redacted turn metadata
            # remains until the 30-day boundary.
            with tempfile.TemporaryDirectory() as second_dir:
                expiry_store = make_store(Path(second_dir))
                expiry_state = empty_state(chat_id=107, now_ms=10_000)
                bundled = transition(
                    expiry_state,
                    add_text("expires", source_message_id=7, now_ms=10_000),
                )
                expiry_store.commit(expiry_state.version, bundled)
                expiry_state = expiry_store.load(107)
                artifact = transition(
                    expiry_state,
                    _action(
                        "artifact_queued",
                        artifact_id="artifact-expire",
                        staging_path="/home/alice/report.pdf",
                        sha256="a" * 64,
                        size=20,
                        now_ms=10_000,
                    ),
                )
                expiry_store.commit(expiry_state.version, artifact)
                expiry_state = expiry_store.load(107)
                finished = transition(
                    expiry_state,
                    _action(
                        "turn_completed",
                        turn_id="turn-retain",
                        session_id="native-1",
                        started_at_ms=10_000,
                        finished_at_ms=10_000,
                        status="completed",
                    ),
                )
                expiry_store.commit(expiry_state.version, finished)
                bundle_id = bundled.state.bundle.bundle_id
                day = expiry_store.prune(10_000 + 24 * 60 * 60 * 1000)
                self.assertNotIn(bundle_id, expiry_store.load(107).bundles)
                self.assertNotIn("artifact-expire", expiry_store.load(107).artifacts)
                self.assertIn("turn-retain", expiry_store.load(107).turns)
                self.assertTrue(day)

                expiry_store.prune(10_000 + 30 * 24 * 60 * 60 * 1000)
                self.assertNotIn("turn-retain", expiry_store.load(107).turns)


def _action(kind: str, **values):
    from telegram_pi_bot.model import ConversationAction

    return ConversationAction(kind=kind, **values)


def _persist_active_turn(store, state, text: str, *, source_message_id: int):
    bundled = transition(
        state,
        add_text(text, source_message_id=source_message_id, now_ms=state.now_ms),
    )
    store.commit(state.version, bundled)
    state = store.load(state.chat_id)
    dispatched = transition(
        state,
        send_now(state.bundle.bundle_id, now_ms=state.now_ms + 1),
    )
    store.commit(state.version, dispatched)
    state = store.load(state.chat_id)
    turn_id = state.active_turn.turn_id
    accepted = transition(
        state,
        _action(
            "bundle_dispatched",
            bundle_id=state.bundle.bundle_id,
            turn_id=turn_id,
            accepted=True,
            now_ms=state.now_ms + 1,
        ),
    )
    store.commit(state.version, accepted)
    return store.load(state.chat_id), turn_id


def _persist_dispatching_with_queued_bundle(root: Path, *, chat_id: int):
    store = make_store(root)
    state = empty_state(chat_id=chat_id, now_ms=1_000)
    first = transition(
        state,
        add_text("message ten", source_message_id=10, now_ms=1_000),
    )
    store.commit(state.version, first)

    state = store.load(chat_id)
    first_bundle_id = state.bundle.bundle_id
    dispatching = transition(
        state,
        send_now(first_bundle_id, now_ms=2_000),
    )
    store.commit(state.version, dispatching)
    state = store.load(chat_id)
    first_effect = next(
        effect
        for effect in store.pending_effects(chat_id)
        if effect.kind == "dispatch_turn"
    )
    if not store.claim_effect(first_effect.effect_id):
        raise AssertionError("message 10 dispatch effect was not claimable")

    second = transition(
        state,
        add_text("message eleven", source_message_id=11, now_ms=2_500),
    )
    store.commit(state.version, second)
    state = store.load(chat_id)
    queued_bundle = state.next_bundle
    if queued_bundle is None:
        raise AssertionError("message 11 was not queued behind message 10")
    timer = transition(
        state,
        _action(
            "bundle_timer_fired",
            bundle_id=queued_bundle.bundle_id,
            generation=queued_bundle.timer_generation,
            now_ms=queued_bundle.due_at_ms,
        ),
    )
    store.commit(state.version, timer)
    state = store.load(chat_id)
    if state.next_bundle is None or state.next_bundle.due_at_ms is not None:
        raise AssertionError("message 11 timer did not queue it without a due time")
    return store, state, first_bundle_id, state.next_bundle.bundle_id, first_effect


if __name__ == "__main__":
    unittest.main()
