from __future__ import annotations

import asyncio
import hashlib
import unittest
from pathlib import Path

from telegram_pi_bot.model import (
    ArtifactReceipt,
    ModelRef,
    RuntimeSnapshot,
    TurnStatus,
    UiRequest,
)
from tests.fakes import FakeSystem


class VerticalSliceTests(unittest.IsolatedAsyncioTestCase):
    async def _system(self) -> FakeSystem:
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        return system

    async def test_authorized_text_reaches_one_pi_turn_and_delivers_one_final(self):
        system = await self._system()
        await system.telegram.receive(text="reply hello", message_id=101)
        await system.clock.advance(seconds=5)
        await system.pi.emit_prompt_accepted()
        await system.pi.emit_text("hello")
        await system.pi.emit_settled()

        self.assertEqual(system.pi.prompts, ["reply hello"])
        self.assertEqual(system.telegram.reactions, [(101, "👀"), (101, "👌")])
        self.assertEqual(system.telegram.final_texts, ["hello"])
        self.assertNotIn("reply hello", _database_text(system.root))
        self.assertNotIn("hello", _database_text(system.root))

    async def test_post_acceptance_disconnect_is_uncertain_and_not_retried(self):
        system = await self._system()
        await system.telegram.receive(text="change files", message_id=102)
        await system.clock.advance(seconds=5)
        await system.pi.emit_prompt_accepted()
        await system.pi.disconnect()
        await system.app.recover()

        self.assertEqual(system.pi.prompt_count, 1)
        state = system.store.load(FakeSystem.CHAT_ID)
        self.assertEqual(next(iter(state.turns.values())).status, "uncertain")
        self.assertEqual(system.telegram.reactions[-1], (102, "😨"))

    async def test_new_session_materializes_only_on_first_model_turn(self):
        system = await self._system()
        await system.telegram.command("/new mobile work")
        pending = next(iter(system.store.load(FakeSystem.CHAT_ID).pending_sessions.values()))
        self.assertFalse(system.pi.session_file_exists(pending.session_id))

        await system.telegram.receive(text="reply hello", message_id=103)
        await system.clock.advance(seconds=5)
        await system.pi.emit_prompt_accepted(disposition="started")
        await system.pi.materialize_session(pending.session_id)
        await system.pi.emit_settled()

        self.assertEqual(system.pi.first_launch_flags()[1], pending.session_id)
        state = system.store.load(FakeSystem.CHAT_ID)
        self.assertNotIn(pending.session_id, state.pending_sessions)
        self.assertEqual(state.selected_session_id, pending.session_id)

    async def test_pending_thinking_change_runs_through_session_operation_lease(self):
        system = await self._system()
        system.pi._snapshots[None] = RuntimeSnapshot(
            models=(ModelRef("ollama", "qwen3.8-orcarouter:latest"),),
            thinking_levels=("off", "low", "medium", "high"),
        )
        await system.telegram.command("/new configuration")

        await system.telegram.command("/thinking high")
        for _ in range(4):
            await asyncio.sleep(0)

        state = system.store.load(FakeSystem.CHAT_ID)
        self.assertIsNone(state.active_turn)
        self.assertEqual(
            state.pending_sessions[state.selected_session_id].thinking,
            "high",
        )
        self.assertEqual(system.pi.prompt_count, 0)
        self.assertIn("configuration updated", system.telegram.final_texts[-1])

    async def test_native_compaction_uses_session_operation_effect(self):
        system = await self._system()
        await system.telegram.command("/new compact me")
        pending = next(
            iter(system.store.load(FakeSystem.CHAT_ID).pending_sessions.values())
        )
        await system.telegram.receive(text="materialize", message_id=122)
        await system.clock.advance(seconds=5)
        await system.pi.materialize_session(pending.session_id)
        await system.pi.emit_text("ready")
        await system.pi.emit_settled()

        await system.telegram.command("/compact keep decisions")
        for _ in range(4):
            await asyncio.sleep(0)

        self.assertEqual(len(system.pi.compactions), 1)
        self.assertEqual(system.pi.compactions[0][1], "keep decisions")
        self.assertIn("10 → 5", system.telegram.final_texts[-1])

    async def test_rejected_photo_can_change_model_then_retry_from_button(self):
        system = await self._system()
        image_model = ModelRef(
            "ollama",
            "vision",
            capabilities=("text", "image"),
        )
        system.pi._snapshots[None] = RuntimeSnapshot(
            models=(image_model,),
            thinking_levels=("off", "medium"),
        )
        system.pi.reject_next = True

        await system.telegram.receive_photo(
            b"\x89PNG\r\n\x1a\nphoto",
            message_id=125,
        )
        await system.clock.advance(seconds=10)
        self.assertEqual(system.store.load(FakeSystem.CHAT_ID).bundle.status, "frozen")

        await system.telegram.command("/model ollama/vision")
        for _ in range(4):
            await asyncio.sleep(0)

        state = system.store.load(FakeSystem.CHAT_ID)
        self.assertEqual(state.bundle.status, "frozen")
        self.assertEqual(
            state.pending_sessions[state.selected_session_id].model_id,
            "vision",
        )
        await system.telegram.press_choice("Send now")
        self.assertEqual(system.pi.requests[-1].session.model_id, "vision")
        self.assertEqual(len(system.pi.requests[-1].content.attachments), 1)

        await system.pi.emit_text("I can see it.")
        await system.pi.emit_settled()
        self.assertEqual(system.telegram.final_texts[-1], "I can see it.")

    async def test_input_during_turn_queues_as_the_next_turn(self):
        system = await self._system()
        await system.telegram.receive(text="first", message_id=104)
        await system.clock.advance(seconds=5)
        await system.telegram.receive(text="second", message_id=105)
        await system.clock.advance(seconds=5)
        self.assertEqual(system.pi.prompts, ["first"])

        await system.pi.emit_text("one")
        await system.pi.emit_settled()
        await system.pi.emit_prompt_accepted()
        self.assertEqual(system.pi.prompts, ["first", "second"])
        await system.pi.emit_text("two")
        await system.pi.emit_settled()
        self.assertEqual(system.telegram.final_texts, ["one", "two"])

    async def test_steer_current_is_explicit_and_reaches_active_turn(self):
        system = await self._system()
        await system.telegram.receive(text="first", message_id=106)
        await system.clock.advance(seconds=5)
        await system.telegram.receive(text="steer this", message_id=107)
        await system.clock.advance(seconds=5)
        queued = system.store.load(FakeSystem.CHAT_ID).next_bundle
        self.assertIsNotNone(queued)

        await system.telegram.press_choice("Steer current")
        self.assertEqual(system.pi.prompt_count, 1)
        self.assertEqual(system.pi.active_turns[0].steered[0].text, "steer this")
        await system.pi.emit_text("done")
        await system.pi.emit_settled()

    async def test_send_now_dispatches_without_waiting_for_bundle_delay(self):
        system = await self._system()
        await system.telegram.receive(text="send immediately", message_id=108)
        await system.telegram.press_choice("Send now")

        self.assertEqual(system.pi.prompts, ["send immediately"])
        await system.pi.emit_text("sent")
        await system.pi.emit_settled()

    async def test_hold_keeps_collecting_until_two_minutes_after_latest_item(self):
        system = await self._system()
        await system.telegram.receive(text="part one", message_id=111)
        await system.telegram.press_choice("Hold 2 min")
        await system.clock.advance(seconds=60)
        await system.telegram.receive(text="part two", message_id=112)
        await system.clock.advance(seconds=119)
        self.assertEqual(system.pi.prompts, [])
        self.assertEqual(
            [label for label, _data in system.telegram.choice_messages[-1][1]],
            ["Send now", "Cancel"],
        )

        await system.clock.advance(seconds=1)
        self.assertEqual(len(system.pi.prompts), 1)
        self.assertIn("part one", system.pi.prompts[0])
        self.assertIn("part two", system.pi.prompts[0])
        await system.pi.emit_text("held")
        await system.pi.emit_settled()

        await system.telegram.receive(text="normal", message_id=113)
        await system.clock.advance(seconds=5)
        self.assertEqual(system.pi.prompts[-1], "normal")

    async def test_stop_aborts_pi_and_freezes_next_bundle(self):
        system = await self._system()
        await system.telegram.receive(text="active", message_id=109)
        await system.clock.advance(seconds=5)
        await system.telegram.receive(text="queued", message_id=110)
        await system.clock.advance(seconds=5)

        await system.telegram.command("/stop")
        state = system.store.load(FakeSystem.CHAT_ID)
        self.assertTrue(system.pi.active_turns[0].aborted)
        self.assertEqual(state.next_bundle.status, "frozen")
        self.assertEqual(system.pi.prompt_count, 1)

    async def test_frozen_bundle_can_be_cancelled_from_telegram_button(self):
        system = await self._system()
        await system.telegram.receive(text="active", message_id=118)
        await system.clock.advance(seconds=5)
        await system.telegram.receive(text="queued", message_id=119)
        await system.telegram.command("/stop")

        self.assertIn(
            "Cancel",
            [label for label, _data in system.telegram.choice_messages[-1][1]],
        )
        await system.telegram.press_choice("Cancel")

        state = system.store.load(FakeSystem.CHAT_ID)
        self.assertIsNone(state.next_bundle)

    async def test_rejected_bundle_shows_retry_and_cancel_buttons(self):
        system = await self._system()
        system.pi.reject_next = True
        await system.telegram.receive(text="retry safely", message_id=120)
        await system.clock.advance(seconds=5)

        state = system.store.load(FakeSystem.CHAT_ID)
        self.assertEqual(state.bundle.status, "frozen")
        self.assertEqual(
            [label for label, _data in system.telegram.choice_messages[-1][1]],
            ["Send now", "Cancel"],
        )
        await system.telegram.press_choice("Cancel")
        self.assertIsNone(system.store.load(FakeSystem.CHAT_ID).bundle)

    async def test_stored_rejection_keeps_buttons_while_next_turn_runs(self):
        system = await self._system()
        system.pi.block_next_start()
        system.pi.reject_next = True
        await system.telegram.receive(text="first rejected", message_id=123)
        await system.clock.advance(seconds=5)
        await system.telegram.receive(text="next runs", message_id=124)
        await system.clock.advance(seconds=5)

        await system.pi.release_start()

        state = system.store.load(FakeSystem.CHAT_ID)
        frozen = [
            bundle for bundle in state.bundles.values() if bundle.status == "frozen"
        ]
        self.assertEqual(len(frozen), 1)
        self.assertEqual(
            [label for label, _data in system.telegram.choice_messages[-1][1]],
            ["Send now", "Cancel"],
        )
        await system.telegram.press_choice("Cancel")
        state = system.store.load(FakeSystem.CHAT_ID)
        self.assertNotIn(frozen[0].bundle_id, state.bundles)
        self.assertIsNotNone(state.active_turn)

    async def test_select_and_confirm_ui_responses_apply_to_current_turn(self):
        system = await self._system()
        await system.telegram.receive(text="ask me", message_id=111)
        await system.clock.advance(seconds=5)
        await system.pi.emit_ui_request(
            UiRequest("request-1", "select", "Pick one", ("A", "B"))
        )
        self.assertEqual(system.telegram.buttons[-1].kind, "select")
        await system.telegram.answer_ui("B")
        self.assertEqual(system.pi.active_turns[0].responses[-1][1].value, "B")

        await system.pi.emit_ui_request(
            UiRequest("request-2", "confirm", "Continue?", ("Yes", "No"))
        )
        self.assertEqual(system.telegram.buttons[-1].kind, "confirm")
        await system.telegram.answer_ui(True)
        self.assertTrue(system.pi.active_turns[0].responses[-1][1].value)
        await system.pi.emit_text("continued")
        await system.pi.emit_settled()

    async def test_blocking_ui_timeout_cancels_the_exact_request(self):
        system = await self._system()
        await system.telegram.receive(text="ask me", message_id=117)
        await system.clock.advance(seconds=5)
        await system.pi.emit_ui_request(
            UiRequest("request-timeout", "confirm", "Continue?", timeout_ms=1_000)
        )

        await system.clock.advance(seconds=1)

        turn = system.pi.active_turns[0]
        self.assertEqual(turn.cancelled_ui, ["request-timeout"])
        self.assertIsNone(system.store.load(FakeSystem.CHAT_ID).blocking_ui)

    async def test_restart_reconstructs_pending_bundle_timer(self):
        system = await self._system()
        await system.telegram.receive(text="survive restart", message_id=112)
        await system.clock.advance(seconds=2)
        await system.app.stop()
        await system.app.start()
        await system.clock.advance(seconds=3)

        self.assertEqual(system.pi.prompts, ["survive restart"])

    async def test_graceful_shutdown_persists_terminal_state_and_freezes_queue(self):
        system = await self._system()
        await system.telegram.receive(text="active", message_id=115)
        await system.clock.advance(seconds=5)
        await system.telegram.receive(text="queued", message_id=116)
        await system.clock.advance(seconds=5)

        await system.app.stop()

        state = system.store.load(FakeSystem.CHAT_ID)
        self.assertIsNone(state.active_turn)
        self.assertEqual(next(iter(state.turns.values())).status, "aborted")
        self.assertEqual(state.next_bundle.status, "frozen")
        self.assertEqual(system.pi.prompt_count, 1)

    async def test_final_telegram_failure_retries_delivery_without_repeating_turn(self):
        system = await self._system()
        system.telegram.fail_text_once = True
        await system.telegram.receive(text="only once", message_id=113)
        await system.clock.advance(seconds=5)
        await system.pi.emit_text("answer")
        await system.pi.emit_settled()

        self.assertEqual(system.pi.prompt_count, 1)
        self.assertEqual(system.telegram.reactions, [(113, "👀")])
        self.assertIn("delivery is pending", system.telegram.final_texts[-1])
        await system.clock.advance(seconds=5)
        self.assertEqual(system.pi.prompt_count, 1)
        self.assertEqual(system.telegram.final_texts[-1], "answer")
        self.assertEqual(system.telegram.reactions[-1], (113, "👌"))

    async def test_one_outbound_artifact_is_delivered_with_final_text(self):
        system = await self._system()
        artifact = system.root / "report.txt"
        artifact.write_text("report payload", encoding="utf-8")
        payload = artifact.read_bytes()
        receipt = ArtifactReceipt(
            "artifact-1",
            "report.txt",
            "file",
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            artifact,
            "Report",
            system.clock.time_ms(),
        )
        await system.telegram.receive(text="create a report", message_id=114)
        await system.clock.advance(seconds=5)
        await system.pi.emit_artifact(receipt)
        await system.pi.emit_text("attached")
        await system.pi.emit_settled()

        self.assertEqual(system.telegram.final_texts, ["attached"])
        self.assertEqual(len(system.telegram.documents), 1)
        self.assertEqual(system.telegram.documents[0][1:], ("report.txt", "Report"))

    async def test_photo_reaches_pi_as_owned_attachment_and_is_cleaned(self):
        system = await self._system()
        payload = b"\x89PNG\r\n\x1a\nphoto"

        await system.telegram.receive_photo(payload, message_id=121)
        await system.clock.advance(seconds=10)

        request = system.pi.requests[0]
        self.assertEqual(len(request.content.attachments), 1)
        staged = Path(request.content.attachments[0])
        self.assertTrue(staged.is_file())
        await system.pi.emit_text("I can see it.")
        await system.pi.emit_settled()
        self.assertFalse(staged.exists())


def _database_text(root: Path) -> str:
    import sqlite3
    from contextlib import closing

    fragments: list[str] = []
    for database_path in root.glob("*.sqlite3"):
        with closing(sqlite3.connect(database_path)) as database:
            for (table,) in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ):
                columns = database.execute(f'PRAGMA table_info("{table}")').fetchall()
                for column in columns:
                    name = column[1]
                    if column[2].upper() in {"TEXT", ""}:
                        fragments.extend(
                            str(row[0])
                            for row in database.execute(
                                f'SELECT "{name}" FROM "{table}" WHERE "{name}" IS NOT NULL'
                            )
                        )
    return "\n".join(fragments)


if __name__ == "__main__":
    unittest.main()
