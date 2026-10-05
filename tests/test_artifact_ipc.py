import asyncio
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from telegram_pi_bot.artifact_ipc import (
    ArtifactBroker,
    ArtifactRejected,
    delete_staged,
    sweep_artifacts,
    verify_staged,
)
from telegram_pi_bot.pi_runtime import PiRuntime
from tests.fakes import (
    RuntimeSettings,
    ScriptedRpcFactory,
    event,
    rpc_response,
    turn_request,
)
from tests.test_artifact_validation import binary_fixture, test_policy


async def send_socket_request(broker, request, *, capability=None):
    reader, writer = await asyncio.open_unix_connection(str(broker.socket_path))
    payload = dict(request)
    payload.setdefault("capability", capability or broker.capability)
    writer.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 2.0)
    writer.close()
    await writer.wait_closed()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


async def start_bound_broker(policy, turn_id):
    broker = await ArtifactBroker.start(policy, turn_id)
    broker.bind_child(os.getpid())
    return broker


async def send_as_different_child(broker, source):
    child_code = (
        "import json,socket,sys; "
        "s=socket.socket(socket.AF_UNIX); "
        "s.connect(sys.argv[1]); "
        "s.sendall((json.dumps({'id':'second-child','capability':sys.argv[2],"
        "'kind':'file','path':sys.argv[3]})+'\\n').encode()); "
        "r=s.recv(4096); print(r.decode() if r else '')"
    )
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_code,
        str(broker.socket_path),
        broker.capability,
        str(source),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await asyncio.wait_for(child.communicate(), 3)
    return json.loads(stdout.decode()) if stdout else None


class ArtifactBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_removes_bound_socket(self):
        with tempfile.TemporaryDirectory() as raw:
            policy = test_policy(Path(raw))
            with mock.patch(
                "telegram_pi_bot.artifact_ipc.os.chmod",
                side_effect=(None, OSError("mode change failed")),
            ):
                with self.assertRaises(OSError):
                    await ArtifactBroker.start(policy, "turn-1")
            self.assertEqual(tuple((policy.state_dir / "ipc").iterdir()), ())

    async def test_runtime_loads_extension_with_only_private_broker_environment(self):
        class FakeBroker:
            socket_path = Path("/tmp/fake-artifact.sock")
            capability = "1" * 64

            def __init__(self):
                self.closed = False
                self.bound_pid = None

            def child_environment(self):
                return {
                    "TELEGRAM_PI_ARTIFACT_SOCKET": str(self.socket_path),
                    "TELEGRAM_PI_ARTIFACT_CAPABILITY": self.capability,
                }

            def bind_child(self, pid):
                self.bound_pid = pid

            def receipts(self):
                return ()

            async def close(self):
                self.closed = True

        created = []

        async def start_broker(_policy, _turn_id):
            broker = FakeBroker()
            created.append(broker)
            return broker

        with tempfile.TemporaryDirectory() as raw:
            script = ScriptedRpcFactory(
                [
                    rpc_response("prompt", {"disposition": "started"}),
                    event("agent_settled"),
                ]
            )
            runtime = PiRuntime(
                RuntimeSettings(),
                rpc_factory=script,
                sessions_dir=Path(raw) / "sessions",
                artifact_policy=test_policy(Path(raw)),
                artifact_broker_factory=start_broker,
            )
            result = await (
                await runtime.start_turn(turn_request("work"), lambda _event: asyncio.sleep(0))
            ).wait()
            self.assertEqual(result.artifacts, ())
            self.assertIn("--extension", script.argv[0])
            self.assertEqual(
                set(script.env[0]),
                {
                    "TELEGRAM_PI_ARTIFACT_SOCKET",
                    "TELEGRAM_PI_ARTIFACT_CAPABILITY",
                },
            )
            self.assertTrue(created[0].closed)
            self.assertEqual(created[0].bound_pid, script.processes[0].child_pid)

    async def test_acceptance_means_bytes_are_validated_hashed_and_staged(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = binary_fixture("pdf", root / "reports")
            broker = await start_bound_broker(test_policy(root), "turn-1")
            try:
                response = await send_socket_request(
                    broker,
                    {"id": "artifact-1", "kind": "file", "path": str(source), "caption": "report"},
                )
                self.assertTrue(response["accepted"])
                receipt = broker.receipts()[0]
                self.assertEqual(receipt.sha256, hashlib.sha256(source.read_bytes()).hexdigest())
                self.assertNotEqual(Path(receipt.staged_path), source)
                self.assertEqual(Path(receipt.staged_path).read_bytes(), source.read_bytes())
                self.assertEqual(stat.S_IMODE(Path(receipt.staged_path).stat().st_mode), 0o600)
            finally:
                await broker.close()

    async def test_socket_is_private_capability_is_fresh_and_close_removes_endpoint(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            broker_a = await ArtifactBroker.start(test_policy(root / "a"), "turn-a")
            broker_b = await ArtifactBroker.start(test_policy(root / "b"), "turn-b")
            path_a, path_b = Path(broker_a.socket_path), Path(broker_b.socket_path)
            try:
                self.assertTrue(stat.S_ISSOCK(path_a.stat().st_mode))
                self.assertEqual(stat.S_IMODE(path_a.stat().st_mode), 0o600)
                self.assertEqual(len(bytes.fromhex(broker_a.capability)), 32)
                self.assertNotEqual(broker_a.capability, broker_b.capability)
            finally:
                await broker_a.close()
                await broker_b.close()
            self.assertFalse(path_a.exists())
            self.assertFalse(path_b.exists())

    async def test_bad_capability_and_second_child_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = binary_fixture("text", root / "report")
            broker = await start_bound_broker(test_policy(root), "turn-1")
            try:
                bad = await send_socket_request(
                    broker,
                    {"id": "bad", "kind": "file", "path": str(source)},
                    capability="0" * 64,
                )
                self.assertFalse(bad["accepted"])
                accepted = await send_socket_request(
                    broker,
                    {"id": "first-child", "kind": "file", "path": str(source)},
                )
                self.assertTrue(accepted["accepted"])

                rejected = await send_as_different_child(broker, source)
                self.assertFalse(rejected["accepted"])
                self.assertEqual(rejected["reason"], "child_mismatch")
            finally:
                await broker.close()

    async def test_bound_child_pid_is_accepted_and_different_same_uid_pid_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = binary_fixture("text", root / "report")
            broker = await ArtifactBroker.start(test_policy(root), "turn-bound-child")
            try:
                broker.bind_child(os.getpid())
                accepted = await send_socket_request(
                    broker,
                    {"id": "bound-child", "kind": "file", "path": str(source)},
                )
                self.assertTrue(accepted["accepted"])

                rejected = await send_as_different_child(broker, source)
                self.assertFalse(rejected["accepted"])
                self.assertEqual(rejected["reason"], "child_mismatch")
                with self.assertRaises(ValueError):
                    broker.bind_child(os.getpid() + 1)
            finally:
                await broker.close()

    async def test_disconnect_does_not_break_owned_child_session(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = binary_fixture("text", root / "report")
            broker = await start_bound_broker(test_policy(root), "turn-1")
            try:
                reader, writer = await asyncio.open_unix_connection(str(broker.socket_path))
                writer.close()
                await writer.wait_closed()
                response = await send_socket_request(
                    broker,
                    {"id": "after-disconnect", "kind": "file", "path": str(source)},
                )
                self.assertTrue(response["accepted"])
            finally:
                await broker.close()

    async def test_overlong_frame_and_idle_client_are_closed_within_bound(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            policy = replace(test_policy(root), ipc_timeout_seconds=0.05, ipc_max_frame_bytes=1024)
            broker = await start_bound_broker(policy, "turn-1")
            try:
                reader, writer = await asyncio.open_unix_connection(str(broker.socket_path))
                writer.write(b"x" * (1024 * 1024))
                try:
                    await writer.drain()
                    self.assertEqual(await asyncio.wait_for(reader.read(), 1.0), b"")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                writer.close()
                try:
                    await writer.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass

                reader, writer = await asyncio.open_unix_connection(str(broker.socket_path))
                self.assertEqual(await asyncio.wait_for(reader.read(), 1.0), b"")
                writer.close()
                await writer.wait_closed()
            finally:
                await broker.close()

    async def test_count_and_aggregate_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            policy = replace(test_policy(root), outbound_total_bytes=1000, outbound_file_bytes=30)
            broker = await start_bound_broker(policy, "turn-1")
            try:
                files = [binary_fixture("text", root / f"r{i}") for i in range(6)]
                accepted = []
                for index, source in enumerate(files):
                    response = await send_socket_request(
                        broker,
                        {"id": f"artifact-{index}", "kind": "file", "path": str(source)},
                    )
                    accepted.append(response["accepted"])
                self.assertEqual(accepted, [True, True, True, True, True, False])
                count_response = await send_socket_request(
                    broker,
                    {"id": "artifact-7", "kind": "file", "path": str(files[0])},
                )
                self.assertEqual(count_response["reason"], "artifact_count_exceeded")

                small_policy = replace(test_policy(root), outbound_total_bytes=24)
                aggregate = await start_bound_broker(small_policy, "turn-aggregate")
                try:
                    big = root / "big.txt"
                    big.write_bytes(b"x" * 18)
                    first = await send_socket_request(aggregate, {"id": "one", "kind": "file", "path": str(big)})
                    second = await send_socket_request(aggregate, {"id": "two", "kind": "file", "path": str(big)})
                    self.assertTrue(first["accepted"])
                    self.assertEqual(second["reason"], "aggregate_size_exceeded")
                finally:
                    await aggregate.close()

                item_policy = replace(
                    test_policy(root), outbound_file_bytes=10
                )
                item = await start_bound_broker(item_policy, "turn-item")
                try:
                    response = await send_socket_request(
                        item,
                        {"id": "too-large", "kind": "file", "path": str(files[0])},
                    )
                    self.assertEqual(response["reason"], "item_size_exceeded")
                finally:
                    await item.close()
            finally:
                await broker.close()

    async def test_staged_hash_recheck_immediate_cleanup_and_retention_sweep(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            policy = test_policy(root)
            source = binary_fixture("text", root / "report")
            broker = await start_bound_broker(policy, "turn-retention")
            try:
                response = await send_socket_request(
                    broker,
                    {"id": "artifact-1", "kind": "file", "path": str(source)},
                )
                self.assertTrue(response["accepted"])
                receipt = broker.receipts()[0]
                verify_staged(receipt)
                Path(receipt.staged_path).write_bytes(b"changed after acceptance")
                with self.assertRaises(ArtifactRejected) as raised:
                    verify_staged(receipt)
                self.assertEqual(raised.exception.reason.value, "source_changed")
                metadata = policy.state_dir / "artifact-metadata" / f"{receipt.artifact_id}.json"
                delete_staged(receipt, policy)
                self.assertFalse(Path(receipt.staged_path).exists())
                self.assertTrue(metadata.exists())
                os.utime(metadata, (2_000_000_000.0,) * 2)
            finally:
                await broker.close()

            broker = await start_bound_broker(policy, "turn-sweep")
            try:
                response = await send_socket_request(
                    broker,
                    {"id": "artifact-2", "kind": "file", "path": str(source)},
                )
                self.assertTrue(response["accepted"])
                receipt = broker.receipts()[0]
            finally:
                await broker.close()
            now = 2_000_000_000.0
            metadata = policy.state_dir / "artifact-metadata" / f"{receipt.artifact_id}.json"
            os.utime(
                receipt.staged_path,
                (now - policy.staging_retention_seconds - 1,) * 2,
            )
            os.utime(
                metadata,
                (now - policy.metadata_retention_seconds - 1,) * 2,
            )
            self.assertEqual(
                sweep_artifacts(policy, now_seconds=now),
                (1, 1),
            )

    async def test_policy_pins_24_hour_staging_and_30_day_metadata_retention(self):
        with tempfile.TemporaryDirectory() as raw:
            policy = test_policy(Path(raw))
            self.assertEqual(policy.staging_retention_seconds, 24 * 60 * 60)
            self.assertEqual(policy.metadata_retention_seconds, 30 * 24 * 60 * 60)
            self.assertEqual(policy.outbound_artifacts_per_turn, 5)
            self.assertEqual(policy.outbound_total_bytes, 50 * 1024 * 1024)
            self.assertEqual(policy.outbound_file_bytes, 20 * 1024 * 1024)
            self.assertEqual(policy.outbound_image_bytes, 10 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
