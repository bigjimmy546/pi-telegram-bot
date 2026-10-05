import asyncio
import unittest

from telegram_pi_bot.model import RuntimeEventKind, TurnStatus, UiRequest, UiResponse
from telegram_pi_bot.pi_protocol import RpcError
from tests.fakes import discard_event, event, rpc_response, scripted_runtime, turn_request


class PiBlockingUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_response_is_bound_to_exact_request_id(self):
        request = UiRequest("dialog-select", "select", "Choose", ("one", "two"))
        runtime, script = scripted_runtime(
            [
                rpc_response("prompt", {"disposition": "started"}),
                event("extension_ui_request", ui_request=request),
            ]
        )
        seen = []

        async def record(item):
            seen.append(item)

        turn = await runtime.start_turn(turn_request("choose"), record)
        self.assertEqual(seen[0].kind, RuntimeEventKind.UI_REQUEST)
        await turn.answer_ui("dialog-select", UiResponse("two"))
        response_command = script.commands[-1]
        self.assertEqual(response_command["id"], "dialog-select")
        self.assertEqual(response_command["value"], "two")
        with self.assertRaises((RpcError, ValueError, LookupError)):
            await turn.answer_ui("dialog-select", UiResponse("one"))
        await turn.abort()

    async def test_confirm_accepts_true_and_false_for_the_live_request(self):
        for answer in (True, False):
            with self.subTest(answer=answer):
                request = UiRequest(f"confirm-{answer}", "confirm", "Continue?")
                runtime, script = scripted_runtime(
                    [
                        rpc_response("prompt", {"disposition": "started"}),
                        event("extension_ui_request", ui_request=request),
                    ]
                )
                turn = await runtime.start_turn(turn_request("confirm"), discard_event)
                await turn.answer_ui(request.request_id, UiResponse(answer))
                self.assertEqual(script.commands[-1]["id"], request.request_id)
                self.assertIs(script.commands[-1]["confirmed"], answer)
                await turn.abort()

    async def test_input_and_editor_requests_are_cancelled_immediately(self):
        runtime, script = scripted_runtime(
            [
                rpc_response("prompt", {"disposition": "started"}),
                event("extension_ui_request", ui_request=UiRequest("input-1", "input", "Text?")),
                event("extension_ui_request", ui_request=UiRequest("editor-1", "editor", "Edit?")),
            ]
        )
        seen = []

        async def record(item):
            seen.append(item)

        turn = await runtime.start_turn(turn_request("unsupported UI"), record)
        self.assertEqual(script.cancelled_dialogs, ["input-1", "editor-1"])
        self.assertFalse(any(item.kind is RuntimeEventKind.UI_REQUEST for item in seen))
        await turn.abort()

    async def test_fire_and_forget_ui_never_becomes_blocking(self):
        runtime, script = scripted_runtime(
            [
                rpc_response("prompt", {"disposition": "started"}),
                event(
                    "extension_ui_request",
                    text="status update",
                    ui_request=UiRequest("notice-1", "notify", "status update"),
                ),
            ]
        )
        seen = []

        async def record(item):
            seen.append(item)

        turn = await runtime.start_turn(turn_request("notify"), record)
        self.assertEqual([item.kind for item in seen], [RuntimeEventKind.PROGRESS])
        self.assertEqual(script.cancelled_dialogs, [])
        await turn.abort()

    async def test_unanswered_ui_expires_and_close_cancels_live_ui(self):
        request = UiRequest("expires", "confirm", "Continue?")
        runtime, script = scripted_runtime(
            [
                rpc_response("prompt", {"disposition": "started"}),
                event("extension_ui_request", ui_request=request),
            ],
            ui_timeout_seconds=0.01,
        )
        turn = await runtime.start_turn(turn_request("expire"), discard_event)
        await asyncio.sleep(0.03)
        self.assertEqual(script.cancelled_dialogs, ["expires"])
        with self.assertRaises(LookupError):
            await turn.answer_ui("expires", UiResponse(True))
        await turn.abort()

        live = UiRequest("restart", "confirm", "Continue?")
        runtime, script = scripted_runtime(
            [
                rpc_response("prompt", {"disposition": "started"}),
                event("extension_ui_request", ui_request=live),
            ]
        )
        turn = await runtime.start_turn(turn_request("restart"), discard_event)
        await turn.close()
        self.assertEqual(script.cancelled_dialogs, ["restart"])

    async def test_composition_can_cancel_only_the_exact_live_ui(self):
        request = UiRequest("delivery-failed", "confirm", "Continue?")
        runtime, script = scripted_runtime(
            [
                rpc_response("prompt", {"disposition": "started"}),
                event("extension_ui_request", ui_request=request),
            ]
        )
        turn = await runtime.start_turn(turn_request("confirm"), discard_event)

        await turn.cancel_ui("stale")
        self.assertEqual(script.cancelled_dialogs, [])
        await turn.cancel_ui(request.request_id)
        self.assertEqual(script.cancelled_dialogs, [request.request_id])
        await turn.cancel_ui(request.request_id)
        self.assertEqual(script.cancelled_dialogs, [request.request_id])
        await turn.abort()

    async def test_stale_or_wrong_process_ui_response_is_rejected(self):
        first_request = UiRequest("first-process", "confirm", "First?")
        first_runtime, _ = scripted_runtime(
            [rpc_response("prompt", {"disposition": "started"}), event("extension_ui_request", ui_request=first_request)]
        )
        first_turn = await first_runtime.start_turn(turn_request("first"), discard_event)
        await first_turn.answer_ui(first_request.request_id, UiResponse(True))
        await first_turn.abort()

        second_request = UiRequest("second-process", "confirm", "Second?")
        second_runtime, _ = scripted_runtime(
            [rpc_response("prompt", {"disposition": "started"}), event("extension_ui_request", ui_request=second_request)]
        )
        second_turn = await second_runtime.start_turn(turn_request("second"), discard_event)
        with self.assertRaises((RpcError, ValueError, LookupError)):
            await second_turn.answer_ui(first_request.request_id, UiResponse(False))
        await second_turn.answer_ui(second_request.request_id, UiResponse(False))
        with self.assertRaises((RpcError, ValueError, LookupError)):
            await second_turn.answer_ui(second_request.request_id, UiResponse(False))
        await second_turn.abort()
        self.assertEqual((await second_turn.wait()).status, TurnStatus.ABORTED)


if __name__ == "__main__":
    unittest.main()
