from __future__ import annotations

import os
import asyncio
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from types import SimpleNamespace

from telegram_pi_bot.app import ProcessLock
from telegram_pi_bot.model import ArtifactReceipt, ConversationAction, NativeSession, NativeSessionRef, UiRequest
from telegram_pi_bot.coordinator import transition
from telegram_pi_bot.store import ControlStore, StoreCorrupt
from telegram_pi_bot.telegram_adapter import TelegramAdapter
from tests.fakes import FakeSystem, FakeTelegram


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def system(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        return system

    async def test_restart_cancels_orphaned_ui_marks_accepted_uncertain_and_freezes_queue(self):
        system = await self.system()
        await system.telegram.receive(text="active", message_id=301)
        await system.clock.advance(seconds=5)
        await system.pi.emit_ui_request(UiRequest("ui-1", "confirm", "Continue?", ("Yes", "No")))
        await system.telegram.receive(text="queued", message_id=302)
        result = system.store.recover(system.clock.now_ms() + 1)
        state = system.store.load(system.CHAT_ID)
        self.assertIsNone(state.blocking_ui)
        self.assertIsNone(state.active_turn)
        self.assertEqual(next(iter(state.turns.values())).status, "uncertain")
        self.assertEqual(state.next_bundle.status, "frozen")
        self.assertTrue(result.replies)
        self.assertEqual(system.pi.prompt_count, 1)
        await system.pi.disconnect()

    async def test_interval_expires_frozen_input_and_incomplete_parts_preserving_project_files(self):
        system = await self.system()
        system.pi.reject_next = True
        await system.telegram.receive(text="expire this prompt", message_id=303)
        await system.clock.advance(seconds=5)
        part = system.app.attachment_store.policy.root / ("a" * 32 + ".png.part")
        part.write_bytes(b"incomplete")
        os.utime(part, ns=(0, 0))
        original = system.root / "project.txt"
        original.write_text("keep")
        await system.clock.advance(seconds=24 * 60 * 60 + 60 * 60)
        self.assertEqual(system.store.load(system.CHAT_ID).bundles, {})
        self.assertFalse(part.exists())
        self.assertTrue(original.exists())

    async def test_retention_continues_after_cleanup_failure_and_redacts_error(self):
        system = await self.system()
        system.pi.reject_next = True
        await system.telegram.receive(text="expire this", message_id=307)
        await system.clock.advance(seconds=5)
        part = system.app.attachment_store.policy.root / ("d" * 32 + ".part")
        part.write_bytes(b"partial")
        os.utime(part, ns=(0, 0))
        original = system.app.attachment_store.sweep
        calls = 0
        def flaky(*args, **values):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("PRIVATE_PATH_TOKEN_SENTINEL")
            return original(*args, **values)
        with patch.object(system.app.attachment_store, "sweep", flaky):
            with self.assertLogs("telegram_pi_bot.app", level="WARNING") as messages:
                await system.clock.advance(seconds=60 * 60)
            self.assertNotIn("PRIVATE_PATH_TOKEN_SENTINEL", "".join(messages.output))
            await system.clock.advance(seconds=25 * 60 * 60)
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(system.store.load(system.CHAT_ID).bundles, {})
        self.assertFalse(part.exists())
        self.assertFalse(system.app._sweeper.done())

    async def test_pending_final_delivery_resumes_without_rerunning_pi(self):
        system = await self.system()
        delivery_id = system.app.delivery.enqueue(turn_id="completed-old", chat_id=system.CHAT_ID, text="retry final", now_ms=1_000)
        await system.app.recover()
        await system.clock.advance(seconds=5)
        self.assertEqual(system.app.delivery.status(delivery_id).status, "delivered")
        self.assertEqual(system.pi.prompt_count, 0)

    async def test_startup_sweeps_expired_bundles_and_parts_before_dispatch(self):
        system = await self.system()
        system.pi.reject_next = True
        await system.telegram.receive(text="expired", message_id=304)
        await system.clock.advance(seconds=5)
        await system.app.stop()
        await system.clock.advance(seconds=24 * 60 * 60 + 1)
        part = system.app.attachment_store.policy.root / ("b" * 32 + ".part")
        part.write_bytes(b"partial")
        os.utime(part, ns=(0, 0))
        await system.app.start()
        self.assertEqual(system.store.load(system.CHAT_ID).bundles, {})
        self.assertFalse(part.exists())
        self.assertEqual(system.pi.prompt_count, 1)

    async def test_duplicate_poller_and_corrupt_sqlite_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = ProcessLock(root / "bot.lock"), ProcessLock(root / "bot.lock")
            first.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
            finally:
                first.release()
            database = root / "control.sqlite3"
            database.write_bytes(b"corrupt database")
            with self.assertRaises(StoreCorrupt):
                ControlStore(database)

    async def test_external_change_warning_is_best_effort_and_does_not_claim_lock(self):
        system = await self.system()
        await system.telegram.receive(text="run", message_id=305)
        await system.clock.advance(seconds=5)
        session_id = system.store.load(system.CHAT_ID).selected_session_id
        await system.pi.materialize_session(session_id)
        modified = False
        async def sessions(limit):
            return [NativeSession(NativeSessionRef(session_id), None, 1_000, 2_000 if modified else 1_000, None)]
        system.pi.list_sessions = sessions
        original_send = system.telegram.send_text
        async def send(chat_id, text):
            nonlocal modified
            if text == "done":
                modified = True
            return await original_send(chat_id, text)
        system.telegram.send_text = send
        await system.pi.emit_text("done")
        await system.pi.emit_settled()
        self.assertIn("best-effort", "\n".join(system.telegram.final_texts))

    async def test_expired_outbound_copy_and_body_are_removed_but_metadata_lasts_30_days(self):
        system = await self.system()
        path = system.root / ("c" * 32 + ".txt")
        path.write_bytes(b"staged copy")
        receipt = ArtifactReceipt("artifact-1", "report.txt", "file", path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest(), path)
        delivery_id = system.app.delivery.enqueue(turn_id="pending-old", chat_id=system.CHAT_ID, text="BODY_SENTINEL", artifacts=(receipt,), now_ms=1_000)
        system.app.delivery.sweep(1_000 + 24 * 60 * 60 * 1_000)
        self.assertFalse(path.exists())
        self.assertEqual(system.app.delivery.status(delivery_id).status, "expired")
        with closing(sqlite3.connect(system.app.delivery.path)) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM delivery_items").fetchone()[0], 0)
        self.assertNotIn(b"BODY_SENTINEL", system.app.delivery.path.read_bytes())
        system.app.delivery.sweep(1_000 + 30 * 24 * 60 * 60 * 1_000)
        with self.assertRaises(ValueError):
            system.app.delivery.status(delivery_id)

    async def test_startup_recovers_session_operation_before_running_pending_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ControlStore(root / "control.sqlite3")
            state = store.load(FakeSystem.CHAT_ID)
            state = store.commit(state.version, transition(state, ConversationAction("new_session", session_id="pending", provider="ollama", model_id="qwen3.8-orcarouter:latest", thinking="medium", now_ms=1)))
            state = store.commit(state.version, transition(state, ConversationAction("begin_session_operation", operation_id="orphan-operation", operation_kind="configure", session_id="pending", update_id=8, now_ms=2)))
            store.commit(state.version, transition(state, ConversationAction("add_text", text="queued", source_message_id=10, now_ms=3)))
            original_react = FakeTelegram.react
            async def slow_react(telegram, *args):
                await asyncio.sleep(0)
                await original_react(telegram, *args)
            with patch.object(FakeTelegram, "react", slow_react):
                system = await FakeSystem.start(root=root)
            try:
                self.assertEqual(system.store.load(system.CHAT_ID).turns["orphan-operation"].status, "uncertain")
                self.assertEqual(system.pi.prompt_count, 0)
                self.assertEqual(system.pi.config_changes, [])
            finally:
                await system.close()

    async def test_final_delivery_expiry_notifies_failure_once_without_rerunning_pi(self):
        system = await self.system()
        await system.telegram.receive(text="one turn", message_id=306)
        await system.clock.advance(seconds=5)
        original_send = system.telegram.send_text
        async def unavailable(chat_id, text):
            if text == "final answer":
                raise RuntimeError("Telegram unavailable")
            return await original_send(chat_id, text)
        system.telegram.send_text = unavailable
        await system.pi.emit_text("final answer")
        await system.pi.emit_settled()
        await system.clock.advance(seconds=25 * 60 * 60)
        await system.app.sweep()
        await system.app.sweep()
        self.assertEqual(system.pi.prompt_count, 1)
        self.assertEqual(system.telegram.reactions[-1], (306, "😨"))
        self.assertEqual(sum("delivery expired" in text for text in system.telegram.final_texts), 1)

    async def test_duplicate_media_is_claimed_before_reaction_and_download_even_after_restart(self):
        system = await self.system()
        downloads = 0
        async def download(file_id, max_bytes):
            nonlocal downloads
            downloads += 1
            yield b"\xff\xd8\xffpayload"
        system.telegram.download_file = download
        def adapter():
            reopened = ControlStore(system.store.path)
            return TelegramAdapter(system.app.config, system.app.dispatch, port=system.telegram,
                claim_update=lambda update_id, now: reopened.claim_update(system.CHAT_ID, update_id, now), now_ms=system.clock.now_ms)
        update = SimpleNamespace(update_id=1001, effective_user=SimpleNamespace(id=system.CHAT_ID), effective_chat=SimpleNamespace(id=system.CHAT_ID, type="private"), callback_query=None, effective_message=SimpleNamespace(message_id=501, text=None, voice=None, photo=[SimpleNamespace(file_id="photo-1", file_size=10)]))
        ingress = adapter()
        await asyncio.gather(ingress.handle_update(update), ingress.handle_update(update))
        reactions = tuple(system.telegram.reactions)
        self.assertEqual(downloads, 1)
        await adapter().handle_update(update)
        self.assertEqual(downloads, 1)
        self.assertEqual(tuple(system.telegram.reactions), reactions)
        self.assertEqual(len(system.store.load(system.CHAT_ID).bundle.items), 1)

    async def test_unauthorized_and_malformed_ingress_do_not_claim_updates(self):
        system = await self.system()
        claims = []
        adapter = TelegramAdapter(system.app.config, system.app.dispatch, port=system.telegram,
            claim_update=lambda update_id, now: claims.append(update_id) or True)
        update = SimpleNamespace(update_id=1002, effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(id=7, type="private"), callback_query=None, effective_message=SimpleNamespace(message_id=501, text="/status"))
        await adapter.handle_update(update)
        update.effective_user.id = system.CHAT_ID
        update.effective_chat.id = system.CHAT_ID
        update.effective_message.text = "/sessions all 51"
        await adapter.handle_update(update)
        self.assertEqual(claims, [])
