import tempfile
import unittest
import uuid
import base64
from pathlib import Path

from telegram_pi_bot.model import (
    ModelRef,
    PendingSession,
    SessionConfig,
    SessionRef,
    TurnContent,
    TurnStatus,
)
from tests.fakes import (
    discard_event,
    event,
    rpc_response,
    scripted_active_runtime,
    scripted_runtime,
    text_delta,
    turn_request,
)


class PiTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_capable_model_receives_validated_base64_payload(self):
        payload = b"\x89PNG\r\n\x1a\nimage"

        def load_image(path):
            self.assertEqual(path, Path("/owned/photo.png"))
            return "image/png", payload

        runtime, script = scripted_runtime(
            [
                rpc_response(
                    "get_state",
                    {
                        "model": {
                            "provider": "ollama",
                            "id": "vision",
                            "input": ["text", "image"],
                        }
                    },
                ),
                rpc_response("prompt", {"disposition": "started"}),
                event("agent_settled"),
            ],
            image_loader=load_image,
        )

        turn = await runtime.start_turn(
            turn_request("describe", attachments=("/owned/photo.png",)),
            discard_event,
        )
        self.assertEqual((await turn.wait()).status, TurnStatus.COMPLETED)
        prompt = next(command for command in script.commands if command["type"] == "prompt")
        self.assertEqual(
            prompt["images"],
            [
                {
                    "type": "image",
                    "data": base64.b64encode(payload).decode("ascii"),
                    "mimeType": "image/png",
                }
            ],
        )

    async def test_text_only_model_rejects_photo_before_prompt_acceptance(self):
        runtime, script = scripted_runtime(
            [
                rpc_response(
                    "get_state",
                    {
                        "model": {
                            "provider": "ollama",
                            "id": "text-only",
                            "input": ["text"],
                        }
                    },
                )
            ],
            image_loader=lambda _path: ("image/png", b"ignored"),
        )

        turn = await runtime.start_turn(
            turn_request("describe", attachments=("/owned/photo.png",)),
            discard_event,
        )
        result = await turn.wait()
        self.assertFalse(turn.accepted)
        self.assertEqual(result.status, TurnStatus.REJECTED)
        self.assertIn("does not accept images", result.text)
        self.assertNotIn("prompt", [command["type"] for command in script.commands])

    async def test_started_prompt_collects_text_until_agent_settled(self):
        runtime, script = scripted_runtime(
            [
                rpc_response("prompt", {"disposition": "started"}),
                text_delta("hello "),
                text_delta("world"),
                event("agent_end"),
                event("agent_settled"),
            ]
        )
        seen = []

        async def record(item):
            seen.append(item)

        turn = await runtime.start_turn(turn_request("say hello"), record)
        self.assertTrue(turn.accepted)
        result = await turn.wait()
        self.assertEqual(result.status, TurnStatus.COMPLETED)
        self.assertEqual(result.text, "hello world")
        self.assertEqual(
            [command["type"] for command in script.commands[:2]],
            ["get_last_assistant_text", "prompt"],
        )
        self.assertEqual(seen[-1].kind, "settled")

    async def test_queued_prompt_waits_for_settlement(self):
        runtime, _script = scripted_runtime(
            [
                rpc_response("prompt", {"disposition": "queued"}),
                event("agent_settled"),
            ]
        )
        turn = await runtime.start_turn(turn_request("queued input"), discard_event)
        self.assertEqual((await turn.wait()).status, TurnStatus.COMPLETED)

    async def test_runtime_error_event_makes_settled_turn_failed(self):
        runtime, _script = scripted_runtime(
            [
                rpc_response("prompt", {"disposition": "started"}),
                event("extension_error"),
                event("agent_settled"),
            ]
        )
        result = await (
            await runtime.start_turn(turn_request("work"), discard_event)
        ).wait()
        self.assertEqual(result.status, TurnStatus.FAILED)
        self.assertFalse(result.retryable)

    async def test_handled_command_does_not_reuse_stale_assistant_text(self):
        runtime, script = scripted_runtime(
            [
                rpc_response("get_last_assistant_text", {"text": "previous answer"}),
                rpc_response("prompt", {"disposition": "handled"}),
                rpc_response("get_last_assistant_text", {"text": "previous answer"}),
            ]
        )
        turn = await runtime.start_turn(turn_request("/extension-command"), discard_event)
        result = await turn.wait()
        self.assertEqual(result.status, TurnStatus.HANDLED)
        self.assertEqual(result.text, "Input was handled without assistant text.")
        self.assertEqual(
            [command["type"] for command in script.commands],
            ["get_last_assistant_text", "prompt", "get_last_assistant_text"],
        )

    async def test_handled_command_returns_new_assistant_text(self):
        runtime, _script = scripted_runtime(
            [
                rpc_response("get_last_assistant_text", {"text": "old"}),
                rpc_response("prompt", {"disposition": "handled"}),
                rpc_response("get_last_assistant_text", {"text": "new output"}),
            ]
        )
        result = await (await runtime.start_turn(turn_request("/command"), discard_event)).wait()
        self.assertEqual(result.status, TurnStatus.HANDLED)
        self.assertEqual(result.text, "new output")

    async def test_unknown_success_disposition_is_uncertain_and_not_retryable(self):
        runtime, _script = scripted_runtime(
            [rpc_response("prompt", {"disposition": "future-value"})]
        )
        result = await (await runtime.start_turn(turn_request("work"), discard_event)).wait()
        self.assertEqual(result.status, TurnStatus.UNCERTAIN)
        self.assertFalse(result.retryable)

    async def test_six_hour_deadline_applies_to_unsettled_accepted_turn(self):
        self.assertEqual(6 * 60 * 60, 21600)
        runtime, _script = scripted_runtime(
            [rpc_response("prompt", {"disposition": "started"})],
            turn_timeout_seconds=0.02,
        )
        result = await (await runtime.start_turn(turn_request("hang"), discard_event)).wait()
        self.assertEqual(result.status, TurnStatus.UNCERTAIN)

    async def test_unexpected_process_end_after_acceptance_is_uncertain(self):
        runtime, script = scripted_runtime(
            [rpc_response("prompt", {"disposition": "started"})]
        )
        turn = await runtime.start_turn(turn_request("work"), discard_event)
        script.fail_process()
        self.assertEqual((await turn.wait()).status, TurnStatus.UNCERTAIN)

    async def test_abort_clears_native_queue_before_abort(self):
        runtime, script = scripted_active_runtime()
        turn = await runtime.start_turn(turn_request("work"), discard_event)
        await turn.steer(TurnContent("change direction"))
        await turn.abort()
        self.assertEqual(
            [command["type"] for command in script.commands[-2:]],
            ["clear_queue", "abort"],
        )

    async def test_failed_queue_clear_terminates_only_owned_child_and_is_uncertain(self):
        runtime, script = scripted_active_runtime(clear_queue="eof")
        turn = await runtime.start_turn(turn_request("work"), discard_event)
        await turn.abort()
        result = await turn.wait()
        self.assertEqual(result.status, TurnStatus.UNCERTAIN)
        self.assertTrue(script.own_child_terminated)
        self.assertFalse(script.external_process_touched)

    async def test_pending_launch_uses_saved_flags_and_reports_only_validated_materialization(self):
        with tempfile.TemporaryDirectory() as raw:
            sessions_dir = Path(raw) / "sessions"
            runtime, script = scripted_runtime(
                [
                    rpc_response("prompt", {"disposition": "started"}),
                    event("agent_settled"),
                ],
                sessions_dir=sessions_dir,
                materialize_on_prompt=True,
            )
            pending = runtime.new_pending_session("draft session")
            self.assertFalse(sessions_dir.exists())
            turn = await runtime.start_turn(turn_request("begin", pending), discard_event)
            result = await turn.wait()
            self.assertEqual(result.materialized_session.ref.id, pending.ref.id)
            self.assertEqual(result.materialized_session.name, "draft session")
            argv = script.argv[0]
            expected = {
                "--session-id": pending.ref.id,
                "--name": "draft session",
                "--provider": pending.config.model.provider,
                "--model": pending.config.model.model_id,
                "--thinking": pending.config.thinking,
            }
            for flag, value in expected.items():
                with self.subTest(flag=flag):
                    self.assertEqual(argv[argv.index(flag) + 1], value)

    async def test_missing_header_leaves_pending_and_later_attempt_reuses_same_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            sessions_dir = Path(raw) / "sessions"
            pending = PendingSession(
                SessionRef("pending", str(uuid.uuid4())),
                "retry me",
                SessionConfig(ModelRef("ollama", "qwen3.8-orcarouter:latest"), "medium"),
                1,
            )
            first, first_script = scripted_runtime(
                [
                    rpc_response("prompt", {"disposition": "started"}),
                    event("agent_settled"),
                ],
                sessions_dir=sessions_dir,
                materialize_on_prompt=False,
            )
            result = await (await first.start_turn(turn_request("first", pending), discard_event)).wait()
            self.assertIsNone(result.materialized_session)

            second, second_script = scripted_runtime(
                [
                    rpc_response("prompt", {"disposition": "started"}),
                    event("agent_settled"),
                ],
                sessions_dir=sessions_dir,
                materialize_on_prompt=True,
            )
            result = await (await second.start_turn(turn_request("retry", pending), discard_event)).wait()
            self.assertEqual(result.materialized_session.ref.id, pending.ref.id)
            for script in (first_script, second_script):
                argv = script.argv[0]
                self.assertEqual(argv[argv.index("--session-id") + 1], pending.ref.id)
                self.assertEqual(argv[argv.index("--provider") + 1], "ollama")
                self.assertEqual(argv[argv.index("--model") + 1], "qwen3.8-orcarouter:latest")
                self.assertEqual(argv[argv.index("--thinking") + 1], "medium")

    async def test_rejected_prompt_does_not_claim_materialized_session(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime, _script = scripted_runtime(
                [rpc_response("prompt", {}, success=False, error="rejected")],
                sessions_dir=Path(raw),
            )
            pending = runtime.new_pending_session()
            turn = await runtime.start_turn(turn_request("retryable", pending), discard_event)
            self.assertFalse(turn.accepted)
            result = await turn.wait()
            self.assertEqual(result.status, TurnStatus.REJECTED)
            self.assertIsNone(result.materialized_session)


if __name__ == "__main__":
    unittest.main()
