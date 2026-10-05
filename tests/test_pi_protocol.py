import asyncio
import sys
import unittest
from pathlib import Path

from telegram_pi_bot.model import RuntimeEventKind, UiResponse
from telegram_pi_bot.pi_protocol import RpcError, RpcProcess, RpcProtocolError, RpcTimeout


FIXTURE = Path(__file__).parent / "fixtures" / "fake_pi_rpc.py"


async def _fake_process(scenario, event_sink=None):
    return await RpcProcess.start(
        [sys.executable, "-u", str(FIXTURE), scenario],
        cwd=Path.cwd(),
        env={},
        event_sink=event_sink or _discard_event,
    )


async def _discard_event(_event):
    pass


class RpcProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_correlates_out_of_order_responses_and_preserves_split_utf8(self):
        events = []
        received_two_events = asyncio.Event()

        async def record(event):
            events.append(event)
            if len(events) == 2:
                received_two_events.set()

        process = await _fake_process("out-of-order", record)
        try:
            first, second = await asyncio.gather(
                process.request({"type": "get_state"}, 1.0),
                process.request({"type": "get_commands"}, 1.0),
            )
            await asyncio.wait_for(received_two_events.wait(), 1.0)
            self.assertEqual(first.command, "get_state")
            self.assertEqual(second.command, "get_commands")
            self.assertEqual(events[0].text, "snow 雪 and separators \u2028 and \u2029")
            self.assertEqual(events[1].text, "after responses")
        finally:
            await process.close()

    async def test_events_can_arrive_before_and_after_a_response(self):
        events = []
        received_two_events = asyncio.Event()

        async def record(event):
            events.append(event)
            if len(events) == 2:
                received_two_events.set()

        process = await _fake_process("events-around-response", record)
        try:
            result = await process.request({"type": "get_state"}, 1.0)
            await asyncio.wait_for(received_two_events.wait(), 1.0)
            self.assertEqual(result.command, "get_state")
            self.assertEqual([event.text for event in events], ["before", "after"])
        finally:
            await process.close()

    async def test_extension_ui_request_is_routed_as_a_typed_event(self):
        events = []
        received = asyncio.Event()

        async def record(event):
            events.append(event)
            received.set()

        process = await _fake_process("extension-ui", record)
        try:
            await process.request({"type": "get_state"}, 1.0)
            await asyncio.wait_for(received.wait(), 1.0)
            self.assertEqual(events[0].kind, RuntimeEventKind.UI_REQUEST)
            self.assertEqual(events[0].ui_request.request_id, "dialog-1")
            self.assertEqual(events[0].ui_request.kind, "select")
        finally:
            await process.close()

    async def test_cancel_dialog_is_fire_and_forget(self):
        process = await _fake_process("cancel-dialog")
        try:
            await asyncio.wait_for(process.cancel_dialog("dialog-1"), 1.0)
            result = await process.request({"type": "get_state"}, 1.0)
            self.assertEqual(result.command, "get_state")
        finally:
            await process.close()

    async def test_typed_dialog_answers_use_pi_response_fields(self):
        process = await _fake_process("answer-dialogs")
        try:
            await process.answer_dialog("select-1", "select", UiResponse("Allow"))
            await process.answer_dialog("confirm-1", "confirm", UiResponse(False))
            result = await process.request({"type": "get_state"}, 1.0)
            self.assertEqual(result.command, "get_state")
            with self.assertRaises(ValueError):
                await process.answer_dialog("bad", "confirm", UiResponse("yes"))
        finally:
            await process.close()

    async def test_timeout_is_typed_and_stderr_diagnostic_is_redacted_and_bounded(self):
        process = await _fake_process("hang-with-secret")
        try:
            with self.assertRaises(RpcTimeout):
                await process.request({"type": "get_state"}, 0.02)
            diagnostic = process.redacted_diagnostic()
            self.assertNotIn("token-sentinel", diagnostic)
            self.assertLessEqual(len(diagnostic.encode("utf-8")), 32 * 1024)
        finally:
            await process.terminate()

    async def test_unexpected_eof_fails_every_pending_request_once(self):
        process = await _fake_process("unexpected-eof")
        terminal = asyncio.create_task(process.wait_for_terminal())
        tasks = [
            asyncio.create_task(process.request({"type": "get_state"}, 1.0)),
            asyncio.create_task(process.request({"type": "get_commands"}, 1.0)),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(result, RpcError) for result in results))
        self.assertIsInstance(await asyncio.wait_for(terminal, 1.0), RpcError)
        with self.assertRaises(RpcError):
            await process.request({"type": "get_state"}, 1.0)
        await process.close()

    async def test_invalid_utf8_json_and_protocol_shapes_are_rejected(self):
        for scenario in (
            "invalid-utf8",
            "invalid-json",
            "invalid-shape",
            "invalid-constant",
            "mismatched-command",
        ):
            with self.subTest(scenario=scenario):
                process = await _fake_process(scenario)
                try:
                    with self.assertRaises(RpcProtocolError):
                        await process.request({"type": "get_state"}, 1.0)
                finally:
                    await process.close()

    async def test_late_response_after_timeout_is_ignored_without_poisoning_process(self):
        process = await _fake_process("late-after-timeout")
        try:
            with self.assertRaises(RpcTimeout):
                await process.request({"type": "get_state"}, 0.01)
            result = await process.request({"type": "get_commands"}, 1.0)
            self.assertEqual(result.command, "get_commands")
        finally:
            await process.close()

    async def test_overlong_stdout_frame_is_rejected(self):
        process = await _fake_process("overlong-stdout")
        try:
            with self.assertRaises(RpcProtocolError):
                await process.request({"type": "get_state"}, 2.0)
        finally:
            await process.terminate()

    async def test_overlong_stderr_remains_bounded_and_redacted(self):
        process = await _fake_process("bounded-stderr")
        try:
            result = await process.request({"type": "get_state"}, 1.0)
            self.assertEqual(result.command, "get_state")
            diagnostic = process.redacted_diagnostic()
            self.assertNotIn("token-sentinel", diagnostic)
            self.assertLessEqual(len(diagnostic.encode("utf-8")), 32 * 1024)
        finally:
            await process.close()

    async def test_close_is_idempotent(self):
        process = await _fake_process("close")
        await process.request({"type": "get_state"}, 1.0)
        await process.close()
        await process.close()

    async def test_terminate_only_affects_the_owned_child(self):
        process = await _fake_process("hang-with-secret")
        unrelated = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)"
        )
        try:
            await process.terminate()
            self.assertIsNone(unrelated.returncode)
        finally:
            await process.close()
            unrelated.terminate()
            while unrelated.returncode is None:
                await asyncio.sleep(0.01)
            await unrelated.wait()


if __name__ == "__main__":
    unittest.main()
