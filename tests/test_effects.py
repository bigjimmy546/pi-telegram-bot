from __future__ import annotations

import unittest

from telegram_pi_bot.model import ConversationAction
from tests.fakes import FakeSystem


class EffectRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_committed_effects_drain_in_order_and_repeat_drain_is_idempotent(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)

        await system.telegram.receive(text="hello", message_id=101)

        state = system.store.load(FakeSystem.CHAT_ID)
        self.assertEqual(state.bundle.items[0].value, "hello")
        self.assertEqual(system.telegram.reactions, [(101, "👀")])
        self.assertTrue(await system.app.drain_effects())
        self.assertEqual(system.pi.prompt_count, 0)

    async def test_timer_effect_dispatches_only_after_five_seconds(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)

        await system.telegram.receive(text="hello", message_id=102)
        await system.clock.advance(seconds=4.999)
        self.assertEqual(system.pi.prompt_count, 0)
        await system.clock.advance(seconds=0.001)
        self.assertEqual(system.pi.prompts, ["hello"])


if __name__ == "__main__":
    unittest.main()
