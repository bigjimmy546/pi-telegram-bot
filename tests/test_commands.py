from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from telegram_pi_bot.model import CompactionResult, ModelRef, NativeSession, NativeSessionRef, RuntimeSnapshot, SessionConfig
from telegram_pi_bot.telegram_ui import BOT_COMMANDS, parse_command
from telegram_pi_bot.telegram_adapter import TelegramAdapter
from telegram_pi_bot.store import ControlStore
from tests.fakes import FakeSystem, expected_pi_ollama_ids, mixed_provider_models, runtime_with_models


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def system(self, **values):
        system = await FakeSystem.start(**values)
        self.addAsyncCleanup(system.close)
        return system

    async def settle_operations(self):
        for _ in range(12):
            await asyncio.sleep(0)

    async def test_skill_catalog_is_context_free_and_direct_skill_is_one_prompt(self):
        system = await self.system(skills=[("advisor", "Investment advisor")])
        await system.telegram.command("/skill")
        self.assertEqual(system.pi.prompt_count, 0)
        self.assertEqual(system.store.load(system.CHAT_ID).pending_sessions, {})
        self.assertEqual([label for label, _ in system.telegram.choice_messages[-1][1]], ["advisor"])
        self.assertIn("Investment advisor", system.telegram.choice_messages[-1][0])
        await system.telegram.command("/skill advisor analyze NVDA")
        await system.clock.advance(seconds=5)
        self.assertEqual(system.pi.prompts, ["/skill:advisor analyze NVDA"])

    async def test_model_catalog_and_agy_profile_are_exact(self):
        system = await self.system(models=mixed_provider_models())
        await system.telegram.command("/new")
        await system.telegram.command("/model")
        text, choices = system.telegram.choice_messages[-1]
        self.assertEqual([label.split(" — ")[0] for label, _ in choices], expected_pi_ollama_ids() + ["antigravity/gemini-3.7-flash"])
        self.assertIn("Ollama Cloud", text)
        self.assertIn("Remote", text)
        await system.telegram.press_choice("antigravity/gemini-3.7-flash — Remote")
        await self.settle_operations()
        state = system.store.load(system.CHAT_ID)
        self.assertEqual(state.pending_sessions[state.selected_session_id].thinking, "high")
        self.assertEqual(system.pi.prompt_count, 0)

    async def test_multiple_pending_sessions_are_numbered_selectable_and_survive_restart(self):
        system = await self.system(models=mixed_provider_models())
        await system.telegram.command("/new first")
        first = system.store.load(system.CHAT_ID).selected_session_id
        await system.telegram.command("/thinking high")
        await self.settle_operations()
        await system.clock.advance(seconds=0.001)
        await system.telegram.command("/new second")
        second = system.store.load(system.CHAT_ID).selected_session_id
        await system.telegram.command("/sessions")
        listing = system.telegram.final_texts[-1]
        self.assertIn("1. pending", listing)
        self.assertIn("not terminal-resumable", listing)
        await system.telegram.command("/use 1")
        self.assertEqual(system.store.load(system.CHAT_ID).selected_session_id, second)
        await system.telegram.command(f"/use {first[:8]}")
        self.assertEqual(system.store.load(system.CHAT_ID).selected_session_id, first)
        restarted = await system.restart()
        self.addAsyncCleanup(restarted.close)
        state = restarted.store.load(system.CHAT_ID)
        self.assertEqual(set(state.pending_sessions), {first, second})
        self.assertEqual(state.pending_sessions[first].name, "first")
        self.assertEqual(state.pending_sessions[first].thinking, "high")
        self.assertEqual(state.pending_sessions[second].thinking, "medium")
        self.assertEqual(state.pending_sessions[first].model_id, "qwen3.8-orcarouter:latest")
        await restarted.telegram.command("/new third")
        self.assertEqual(len(restarted.store.load(system.CHAT_ID).pending_sessions), 3)

    async def test_hidden_aliases_are_non_destructive_and_help_lists_visible_commands(self):
        system = await self.system()
        for command in ("/new named", "/clear", "/reset"):
            await system.telegram.command(command)
        self.assertEqual(len(system.store.load(system.CHAT_ID).pending_sessions), 3)
        for command in ("/help", "/start"):
            await system.telegram.command(command)
            text = system.telegram.final_texts[-1]
            for entry in BOT_COMMANDS:
                self.assertIn("/" + entry.command, text)
            self.assertIn("sequential", text)
            self.assertIn("blocking", text)
        self.assertNotIn("doctor", [entry.command for entry in BOT_COMMANDS])

    async def test_status_uses_pending_settings_and_reports_queue_ui_context_delivery(self):
        system = await self.system(models=mixed_provider_models())
        from dataclasses import replace
        system.pi._snapshots[None] = replace(system.pi._snapshots[None], session_stats={"tokens": 10, "compactions": 3, "cost": None, "contextUsage.tokens": 123, "contextUsage.percent": 25, "contextUsage.contextWindow": 492})
        await system.telegram.command("/new named")
        await system.telegram.command("/status")
        text = system.telegram.final_texts[-1]
        for value in ("named", "pending", "qwen3.8-orcarouter:latest", "medium", "context:", "queued:", "blocking UI:", "delivery:"):
            self.assertIn(value, text)
        await system.telegram.command("/usage")
        self.assertIn("tokens:", system.telegram.final_texts[-1])
        self.assertIn("compactions: 3", system.telegram.final_texts[-1])
        self.assertIn("context: 123 tokens; 25% of 492", text)

    async def test_active_turn_refuses_new_use_model_thinking_and_compact(self):
        system = await self.system(models=mixed_provider_models())
        await system.telegram.command("/new other")
        other = system.store.load(system.CHAT_ID).selected_session_id
        await system.telegram.command("/new active")
        selected = system.store.load(system.CHAT_ID).selected_session_id
        await system.telegram.receive(text="run", message_id=201)
        await system.clock.advance(seconds=5)
        for command in ("/new forbidden", f"/use {other}", "/model ollama/vision", "/thinking high", "/compact"):
            await system.telegram.command(command)
        state = system.store.load(system.CHAT_ID)
        self.assertEqual(state.selected_session_id, selected)
        self.assertEqual(len(state.pending_sessions), 2)
        self.assertEqual(system.pi.config_changes, [])
        self.assertEqual(system.pi.compactions, [])
        await system.telegram.command("/stop")
        self.assertTrue(system.pi.active_turns[0].aborted)

    async def test_compact_pending_and_failed_native_do_not_claim_reduction(self):
        system = await self.system()
        await system.telegram.command("/new")
        await system.telegram.command("/compact")
        self.assertIn("nothing to compact", system.telegram.final_texts[-1])
        pending = system.store.load(system.CHAT_ID).selected_session_id
        await system.telegram.receive(text="native", message_id=202)
        await system.clock.advance(seconds=5)
        await system.pi.materialize_session(pending)
        await system.pi.emit_text("done")
        await system.pi.emit_settled()
        async def failed(*args):
            return CompactionResult(False, 10, 10)
        system.pi.compact = failed
        await system.telegram.command("/compact keep decisions")
        await self.settle_operations()
        self.assertNotIn("Compacted:", system.telegram.final_texts[-1])

    async def test_bundle_controls_edit_one_message_and_old_generation_fails_closed(self):
        system = await self.system()
        await system.telegram.receive(text="first", message_id=203)
        old = system.telegram.choice_messages[-1][1][0][1]
        await system.telegram.receive(text="second", message_id=204)
        self.assertEqual(len(system.telegram.choice_messages), 1)
        self.assertEqual(len(system.telegram.choice_edits), 1)
        await system.telegram.callback(old)
        self.assertEqual(system.pi.prompt_count, 0)
        await system.telegram.press_choice("Send now")
        self.assertEqual(system.pi.prompts, ["first\n\nsecond"])

    async def test_catalog_refresh_failure_invalidates_previous_buttons_and_no_fallback(self):
        system = await self.system(models=mixed_provider_models())
        await system.telegram.command("/new")
        await system.telegram.command("/model")
        old = system.telegram.choice_messages[-1][1][-1][1]
        async def unavailable(*args):
            raise RuntimeError("provider unavailable")
        system.pi.inspect = unavailable
        await system.telegram.command("/model")
        await system.telegram.callback(old)
        self.assertEqual(system.store.load(system.CHAT_ID).pending_sessions[system.store.load(system.CHAT_ID).selected_session_id].provider, "ollama")
        self.assertEqual(system.pi.prompt_count, 0)

    async def test_sessions_native_limit_prefix_ambiguity_and_stale_list(self):
        system = await self.system()
        native = [NativeSession(NativeSessionRef(value), None, 1, 1, None) for value in ("abc-one", "abc-two")]
        limits = []
        async def sessions(limit):
            limits.append(limit)
            return native[:limit]
        system.pi.list_sessions = sessions
        await system.telegram.command("/sessions all 50")
        self.assertEqual(limits[-1], 50)
        await system.telegram.command("/use abc")
        self.assertIsNone(system.store.load(system.CHAT_ID).selected_session_id)
        await system.telegram.command("/use 2")
        self.assertEqual(system.store.load(system.CHAT_ID).selected_session_id, "abc-two")
        native.clear()
        await system.telegram.command("/use 1")
        self.assertEqual(system.store.load(system.CHAT_ID).selected_session_id, "abc-two")
        for command in ("/sessions all 0", "/sessions all 51", "/sessions 20"):
            self.assertIsNone(parse_command(command))

    async def test_use_checks_complete_inventory_not_just_display_limit(self):
        system = await self.system()
        ids = ["matching-new"] + [f"other-{index}" for index in range(49)] + ["matching-old"]
        native = [NativeSession(NativeSessionRef(value), None, 1, len(ids) - index, None) for index, value in enumerate(ids)]
        runtime, _factory = runtime_with_models(mixed_provider_models())
        runtime._discover_sessions = lambda: native.copy()
        system.pi.list_sessions = runtime.list_sessions
        await system.telegram.command("/sessions all 50")
        self.assertNotIn("matching-old", system.telegram.final_texts[-1])
        await system.telegram.command("/use matching")
        self.assertIsNone(system.store.load(system.CHAT_ID).selected_session_id)
        await system.telegram.command("/use matching-old")
        self.assertEqual(system.store.load(system.CHAT_ID).selected_session_id, "matching-old")

    async def test_materialization_replaces_only_matching_pending_session(self):
        system = await self.system()
        await system.telegram.command("/new untouched")
        untouched = system.store.load(system.CHAT_ID).selected_session_id
        await system.telegram.command("/new matching")
        matching = system.store.load(system.CHAT_ID).selected_session_id
        await system.telegram.receive(text="materialize", message_id=205)
        await system.clock.advance(seconds=5)
        await system.pi.materialize_session(matching)
        await system.pi.emit_text("done")
        await system.pi.emit_settled()
        state = system.store.load(system.CHAT_ID)
        self.assertEqual(set(state.pending_sessions), {untouched})
        self.assertEqual(state.selected_session_id, matching)

    async def test_pending_non_default_model_metadata_uses_its_launch_settings(self):
        runtime, factory = runtime_with_models(mixed_provider_models())
        config = SessionConfig(ModelRef("antigravity", "gemini-3.7-flash"), "high")
        await runtime.inspect(None, config=config)
        argv = factory.argv[-1]
        self.assertIn("--no-session", argv)
        self.assertEqual(argv[argv.index("--provider") + 1], "antigravity")
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.7-flash")
        self.assertEqual(argv[argv.index("--thinking") + 1], "high")
        self.assertEqual(factory.prompt_count, 0)

    async def test_thinking_catalog_and_change_use_pending_selected_model_levels(self):
        system = await self.system(models=mixed_provider_models())
        await system.telegram.command("/new")
        await system.telegram.command("/model antigravity/gemini-3.7-flash")
        await self.settle_operations()
        from dataclasses import replace
        original = system.pi.inspect
        async def inspect(session, *, config=None):
            snapshot = await original(session)
            levels = ("high",) if config and config.model.provider == "antigravity" else ("off", "medium")
            return replace(snapshot, thinking_levels=levels)
        system.pi.inspect = inspect
        await system.telegram.command("/thinking")
        self.assertEqual([label for label, _ in system.telegram.choice_messages[-1][1]], ["high"])
        await system.telegram.command("/thinking off")
        await self.settle_operations()
        pending = system.store.load(system.CHAT_ID).pending_sessions
        self.assertEqual(next(iter(pending.values())).thinking, "high")

    async def test_concurrent_and_restart_duplicate_commands_have_no_extra_metadata_or_reply(self):
        system = await self.system()
        calls = 0
        original = system.pi.inspect
        async def inspect(*args, **values):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return await original(*args, **values)
        system.pi.inspect = inspect
        def adapter():
            reopened = ControlStore(system.store.path)
            return TelegramAdapter(system.app.config, system.app.dispatch, port=system.telegram,
                claim_update=lambda update_id, now: reopened.claim_update(system.CHAT_ID, update_id, now), now_ms=system.clock.now_ms)
        update = SimpleNamespace(update_id=999, effective_user=SimpleNamespace(id=system.CHAT_ID), effective_chat=SimpleNamespace(id=system.CHAT_ID, type="private"), callback_query=None, effective_message=SimpleNamespace(message_id=500, text="/status"))
        ingress = adapter()
        await asyncio.gather(ingress.handle_update(update), ingress.handle_update(update))
        self.assertEqual(calls, 1)
        self.assertEqual(len(system.telegram.final_texts), 1)
        await adapter().handle_update(update)
        self.assertEqual(calls, 1)
        self.assertEqual(len(system.telegram.final_texts), 1)

    async def test_native_session_selection_restores_each_live_model_and_thinking(self):
        from dataclasses import replace
        system = await self.system(models=mixed_provider_models())
        catalog = system.pi._snapshots[None]
        first, second = NativeSessionRef("native-first"), NativeSessionRef("native-second")
        system.pi._snapshots[first.id] = replace(catalog, selected_model=catalog.models[0], thinking="low", session_id=first.id)
        system.pi._snapshots[second.id] = replace(catalog, selected_model=catalog.models[1], thinking="medium", session_id=second.id)
        async def sessions(limit):
            return [NativeSession(ref, None, 1, 1, None) for ref in (first, second)][:limit]
        system.pi.list_sessions = sessions
        await system.telegram.command("/sessions")
        await system.telegram.command("/use 1")
        await system.telegram.command("/model antigravity/gemini-3.7-flash")
        await self.settle_operations()
        self.assertEqual(system.pi.config_changes[-1][1].thinking, "high")
        await system.telegram.command("/use 2")
        await system.telegram.command("/status")
        self.assertIn("ollama/vision", system.telegram.final_texts[-1])
        self.assertIn("thinking: medium", system.telegram.final_texts[-1])
        await system.telegram.command(f"/use {first.id}")
        await system.telegram.command("/status")
        self.assertIn("antigravity/gemini-3.7-flash", system.telegram.final_texts[-1])
        self.assertIn("thinking: high", system.telegram.final_texts[-1])
        self.assertEqual(system.pi.prompt_count, 0)

    async def test_skill_catalog_paging_and_button_remain_context_free(self):
        system = await self.system(skills=[(f"skill{index}", "Description") for index in range(11)])
        await system.telegram.command("/skill")
        self.assertIn("Next", [label for label, _ in system.telegram.choice_messages[-1][1]])
        await system.telegram.press_choice("Next")
        self.assertIn("skill10", [label for label, _ in system.telegram.choice_messages[-1][1]])
        await system.telegram.press_choice("skill10")
        self.assertIn("/skill skill10 <request>", system.telegram.final_texts[-1])
        self.assertEqual(system.pi.prompt_count, 0)
        self.assertEqual(system.store.load(system.CHAT_ID).pending_sessions, {})
