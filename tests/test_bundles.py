from __future__ import annotations

import unittest

from telegram_pi_bot.coordinator import transition
from tests.fakes import add_photo, add_text, effect_kinds, empty_state


class BundleTests(unittest.TestCase):
    def test_new_input_replaces_bundle_timer_and_only_latest_timer_dispatches(self):
        first = transition(
            empty_state(now_ms=1_000), add_text("first", source_message_id=1)
        )
        old_bundle_id = first.state.bundle.bundle_id
        old_generation = first.state.bundle.timer_generation

        extended = transition(
            first.state, add_text("second", source_message_id=2, now_ms=2_000)
        )
        self.assertEqual(extended.state.bundle.due_at_ms, 7_000)
        self.assertEqual(
            effect_kinds(extended), ["cancel_timer", "schedule_timer", "react"]
        )

        stale = transition(
            extended.state,
            _action(
                "bundle_timer_fired",
                bundle_id=old_bundle_id,
                generation=old_generation,
                now_ms=6_000,
            ),
        )
        self.assertEqual(effect_kinds(stale), [])
        self.assertEqual(stale.state.bundle.due_at_ms, 7_000)

    def test_media_converts_existing_bundle_to_ten_second_window(self):
        text_bundle = transition(
            empty_state(now_ms=1_000), add_text("caption", source_message_id=10)
        )
        converted = transition(
            text_bundle.state,
            add_photo("photo", source_message_id=11, now_ms=2_000),
        )
        self.assertEqual(converted.state.bundle.due_at_ms, 12_000)
        self.assertEqual(converted.state.bundle.kind, "media")

    def test_current_timer_dispatches_only_after_due_time(self):
        bundled = transition(
            empty_state(now_ms=1_000), add_text("ready", source_message_id=10)
        )
        bundle = bundled.state.bundle
        early = transition(
            bundled.state,
            _action(
                "bundle_timer_fired",
                bundle_id=bundle.bundle_id,
                generation=bundle.timer_generation,
                now_ms=bundle.due_at_ms - 1,
            ),
        )
        self.assertEqual(effect_kinds(early), [])

        due = transition(
            early.state,
            _action(
                "bundle_timer_fired",
                bundle_id=bundle.bundle_id,
                generation=bundle.timer_generation,
                now_ms=bundle.due_at_ms,
            ),
        )
        self.assertIn("dispatch_turn", effect_kinds(due))


def _action(kind: str, **values):
    from telegram_pi_bot.model import ConversationAction

    return ConversationAction(kind=kind, **values)


if __name__ == "__main__":
    unittest.main()
