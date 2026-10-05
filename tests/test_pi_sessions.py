import json
import tempfile
import unittest
import uuid
from pathlib import Path

from telegram_pi_bot.model import ModelRef, NativeSessionRef, SessionConfigChange
from telegram_pi_bot.pi_protocol import RpcError, RpcResponse
from tests.fakes import (
    FakeRpcFactory,
    RuntimeSettings,
    model,
    runtime_with_models,
)


def _header(session_id, *, timestamp="2026-10-01T12:00:00Z", cwd="/home/alice", version=3, **extra):
    value = {
        "type": "session",
        "version": version,
        "id": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
    }
    value.update(extra)
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")


def _runtime_for_dir(sessions_dir, *, responses=None, command_overrides=None):
    from telegram_pi_bot.pi_runtime import PiRuntime

    base = {
        "get_state": {
            "model": {"provider": "ollama", "id": "qwen3.8-orcarouter:latest"},
            "thinkingLevel": "medium",
        },
        "get_available_models": {
            "models": [
                {
                    "provider": "ollama",
                    "id": "qwen3.8-orcarouter:latest",
                    "input": ["text", "image"],
                    "contextWindow": 32768,
                    "maxTokens": 8192,
                    "cost": {"input": 0, "output": 0},
                },
                {
                    "provider": "antigravity",
                    "id": "gemini-3.7-flash",
                    "input": ["text", "image"],
                    "contextWindow": 32768,
                    "maxTokens": 8192,
                    "cost": {"input": 0, "output": 0},
                },
            ]
        },
        "get_available_thinking_levels": {"levels": ["off", "low", "medium", "high"]},
        "get_commands": {"commands": []},
        "get_session_stats": {"tokens": 100, "compactions": 0, "cost": None},
        "compact": {"tokensBefore": 100, "estimatedTokensAfter": 40},
    }
    if responses:
        base.update(responses)
    factory = FakeRpcFactory(base, command_overrides=command_overrides)
    runtime = PiRuntime(
        RuntimeSettings(), rpc_factory=factory, sessions_dir=Path(sessions_dir)
    )
    return runtime, factory


class PiSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_documented_v3_headers_newest_first_without_reading_bodies(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            oldest = uuid.uuid4().hex
            newest = uuid.uuid4().hex
            (root / f"{oldest}.jsonl").write_bytes(
                _header(oldest, timestamp="2026-10-01T10:00:00Z")
                + b"\xffprivate message body sentinel\n"
            )
            (root / f"{newest}.jsonl").write_bytes(
                _header(newest, timestamp="2026-10-02T10:00:00Z")
                + b"private message body sentinel\n"
            )
            runtime, fake = _runtime_for_dir(root)
            sessions = await runtime.list_sessions(10)
            self.assertEqual([session.ref.id for session in sessions], [newest, oldest])
            self.assertIsNone(sessions[0].name)
            self.assertNotIn("private message body sentinel", repr(sessions))
            self.assertEqual(fake.processes, [])

    async def test_rejects_wrong_cwd_malformed_non_v3_symlink_nonregular_and_oversized(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            valid_id = uuid.uuid4().hex
            wrong_cwd = uuid.uuid4().hex
            malformed = uuid.uuid4().hex
            old_version = uuid.uuid4().hex
            oversized = uuid.uuid4().hex
            nonregular = uuid.uuid4().hex
            symlink_id = uuid.uuid4().hex

            (root / f"{valid_id}.jsonl").write_bytes(_header(valid_id))
            (root / f"{wrong_cwd}.jsonl").write_bytes(
                _header(wrong_cwd, cwd="/tmp/elsewhere")
            )
            (root / f"{malformed}.jsonl").write_bytes(b"{not-json}\n")
            (root / f"{old_version}.jsonl").write_bytes(
                _header(old_version, version=2)
            )
            (root / f"{oversized}.jsonl").write_bytes(
                b'{"type":"session","version":3,"id":"' + b"x" * (64 * 1024) + b'"}\n'
            )
            (root / f"{nonregular}.jsonl").mkdir()
            target = root / "target"
            target.write_bytes(_header(symlink_id))
            (root / f"{symlink_id}.jsonl").symlink_to(target)

            runtime, _fake = _runtime_for_dir(root)
            sessions = await runtime.list_sessions(50)
            self.assertEqual([session.ref.id for session in sessions], [valid_id])

    async def test_session_limit_is_restricted_to_one_through_fifty(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime, _fake = _runtime_for_dir(raw)
            for limit in (0, 51, -1):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    await runtime.list_sessions(limit)
            self.assertEqual(await runtime.list_sessions(1), [])
            self.assertEqual(await runtime.list_sessions(50), [])

    async def test_duplicate_header_ids_are_not_listed_or_selected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            duplicate_id = uuid.uuid4().hex
            (root / "first.jsonl").write_bytes(_header(duplicate_id))
            (root / "second.jsonl").write_bytes(_header(duplicate_id))
            runtime, _fake = _runtime_for_dir(root)
            self.assertEqual(await runtime.list_sessions(50), [])
            with self.assertRaises(LookupError):
                await runtime.inspect(NativeSessionRef(duplicate_id))

    async def test_pending_allocation_is_pure_and_applies_antigravity_high_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime, fake = _runtime_for_dir(raw)
            pending = runtime.new_pending_session("analysis")
            self.assertEqual(pending.name, "analysis")
            self.assertTrue(uuid.UUID(pending.ref.id))
            self.assertEqual(pending.config.model, ModelRef("ollama", "qwen3.8-orcarouter:latest"))
            self.assertGreaterEqual(len(pending.config.thinking), 1)
            self.assertEqual(fake.processes, [])
            self.assertEqual(list(Path(raw).iterdir()), [])

    async def test_configuration_validates_full_change_before_mutating_native_session(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime, fake = _runtime_for_dir(raw)
            native_ref = NativeSessionRef(uuid.uuid4().hex)
            (Path(raw) / f"{native_ref.id}.jsonl").write_bytes(_header(native_ref.id))
            with self.assertRaises((ValueError, LookupError)):
                await runtime.configure_session(
                    native_ref,
                    SessionConfigChange(
                        model=ModelRef("openrouter", "not-allowed"), thinking="impossible"
                    ),
                )
            self.assertFalse(
                any(command["type"] in {"set_model", "set_thinking_level", "set_session_name"}
                    for command in fake.commands)
            )

    async def test_antigravity_configuration_sets_high_thinking(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime, fake = _runtime_for_dir(raw)
            native_ref = NativeSessionRef(uuid.uuid4().hex)
            (Path(raw) / f"{native_ref.id}.jsonl").write_bytes(_header(native_ref.id))
            snapshot = await runtime.configure_session(
                native_ref,
                SessionConfigChange(model=ModelRef("antigravity", "gemini-3.7-flash")),
            )
            self.assertEqual(snapshot.thinking_levels, ("off", "low", "medium", "high"))
            self.assertTrue(
                any("high" in argv for argv in fake.argv),
                "Antigravity selection must launch Pi with high thinking",
            )
            self.assertEqual(fake.commands[-1]["type"], "get_state")

    async def test_compaction_success_parses_token_estimate_and_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime, _fake = _runtime_for_dir(raw)
            native_ref = NativeSessionRef(uuid.uuid4().hex)
            (Path(raw) / f"{native_ref.id}.jsonl").write_bytes(_header(native_ref.id))
            result = await runtime.compact(native_ref, "keep decisions")
            self.assertTrue(result.success)
            self.assertEqual(result.tokens_before, 100)
            self.assertEqual(result.estimated_tokens_after, 40)

        with tempfile.TemporaryDirectory() as raw:
            failed = RpcResponse("fake-failure", "compact", False, None, "compaction failed")
            runtime, _fake = _runtime_for_dir(
                raw, command_overrides={"compact": [failed]}
            )
            native_ref = NativeSessionRef(uuid.uuid4().hex)
            (Path(raw) / f"{native_ref.id}.jsonl").write_bytes(_header(native_ref.id))
            with self.assertRaises(RpcError):
                await runtime.compact(native_ref, None)


if __name__ == "__main__":
    unittest.main()
