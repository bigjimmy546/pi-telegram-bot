"""Explicit installed-Pi compatibility probes; excluded from unit discovery."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path

from telegram_pi_bot.artifact_ipc import ArtifactBroker, ArtifactPolicy
from telegram_pi_bot.pi_protocol import RpcEvent, RpcProcess


def _default_session_root() -> Path:
    import re

    home = Path.home()
    encoded = "--" + re.sub(r"[/\\:]", "-", str(home).lstrip("/\\")) + "--"
    return home / ".pi/agent/sessions" / encoded


PI_SESSION_ROOT = _default_session_root()


def _session_files() -> tuple[tuple[str, int, int], ...]:
    try:
        return tuple(
            sorted(
                (path.name, path.stat().st_size, path.stat().st_mtime_ns)
                for path in PI_SESSION_ROOT.iterdir()
                if path.is_file() and not path.is_symlink()
            )
        )
    except OSError as error:
        raise RuntimeError("Pi session inventory is unavailable") from error


async def metadata() -> None:
    before = _session_files()

    async def discard_event(_event: RpcEvent) -> None:
        return

    version_process = await asyncio.create_subprocess_exec(
        "/usr/bin/pi",
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(version_process.communicate(), 30)
    except TimeoutError:
        version_process.kill()
        await version_process.wait()
        raise RuntimeError("Pi version check timed out") from None
    if version_process.returncode != 0:
        raise RuntimeError("Pi version check failed")
    version = stdout.decode("utf-8", errors="strict").strip()
    if not version or len(version) > 200:
        raise RuntimeError("Pi version output is invalid")

    process = await RpcProcess.start(
        [
            "/usr/bin/pi",
            "--mode",
            "rpc",
            "--no-session",
            "--offline",
            "--approve",
            "--no-tools",
            "--provider",
            "ollama",
            "--model",
            "qwen3.8-orcarouter:latest",
            "--thinking",
            "medium",
        ],
        cwd=Path.home(),
        env={},
        event_sink=discard_event,
    )
    try:
        records: dict[str, Mapping[str, object]] = {}
        for command in (
            "get_state",
            "get_available_models",
            "get_available_thinking_levels",
            "get_commands",
            "get_session_stats",
        ):
            response = await process.request({"type": command}, 30)
            if not response.success or not isinstance(response.data, Mapping):
                raise RuntimeError(f"Pi metadata command failed: {command}")
            records[command] = response.data
    finally:
        await process.close()
    if _session_files() != before:
        raise RuntimeError("metadata probe changed Pi session storage")
    models = records["get_available_models"].get("models")
    levels = records["get_available_thinking_levels"].get("levels")
    commands = records["get_commands"].get("commands")
    if not isinstance(models, tuple) or not isinstance(levels, tuple) or not isinstance(commands, tuple):
        shapes = {
            "models": type(models).__name__,
            "thinking_levels": type(levels).__name__,
            "commands": type(commands).__name__,
        }
        raise RuntimeError(f"Pi metadata response shape is invalid: {shapes}")
    print(
        json.dumps(
            {
                "pi_version": version,
                "models": len(models),
                "thinking_levels": len(levels),
                "commands": len(commands),
                "session_storage_unchanged": True,
                "prompt_sent": False,
                "telegram_contacted": False,
            },
            separators=(",", ":"),
        )
    )


async def _run_tool_probe(
    process: RpcProcess,
    events: asyncio.Queue[RpcEvent],
    *,
    path: Path,
    expected_accepted: bool,
) -> dict[str, object]:
    prompt = (
        "Call the send_file tool exactly once now with path "
        f"{json.dumps(str(path))} and caption \"probe\". "
        "Do not call send_image or any other tool. After the tool returns, "
        "reply with one short sentence."
    )
    response = await process.request(
        {"type": "prompt", "message": prompt},
        30,
    )
    if not response.success or not isinstance(response.data, Mapping):
        raise RuntimeError("Pi rejected the artifact probe prompt")
    if response.data.get("disposition") != "started":
        raise RuntimeError("Pi did not start the artifact probe turn")

    tool_results: list[dict[str, object]] = []
    async with asyncio.timeout(5 * 60):
        while True:
            event = await events.get()
            if event.source_type == "tool_execution_end":
                payload = dict(event.payload)
                if payload.get("toolName") != "send_file":
                    raise RuntimeError("Pi invoked an unexpected artifact tool")
                tool_results.append(payload)
            if event.source_type == "agent_settled":
                break
    if len(tool_results) != 1:
        raise RuntimeError("Pi did not invoke send_file exactly once")
    result = tool_results[0].get("result")
    if not isinstance(result, Mapping):
        raise RuntimeError("send_file returned no structured result")
    details = result.get("details")
    if not isinstance(details, Mapping) or details.get("accepted") is not expected_accepted:
        reason = details.get("reason", "missing") if isinstance(details, Mapping) else "missing"
        raise RuntimeError(f"send_file returned the wrong acceptance result: {reason}")
    if tool_results[0].get("isError") is expected_accepted:
        raise RuntimeError("send_file returned the wrong Pi error state")
    return dict(details)


async def artifact_extension_only() -> None:
    extension = (
        Path(__file__).parents[1]
        / "src"
        / "telegram_pi_bot"
        / "extensions"
        / "telegram_artifacts.ts"
    )
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "probe-report.txt"
        source.write_text("artifact extension probe\n", encoding="utf-8")
        policy = ArtifactPolicy(
            allowed_root=root,
            state_dir=root / "state",
            staging_dir=root / "state" / "artifacts" / "staging",
        )
        broker = await ArtifactBroker.start(policy, "live-probe")
        try:
            events: asyncio.Queue[RpcEvent] = asyncio.Queue()

            async def record_event(event: RpcEvent) -> None:
                await events.put(event)

            process = await RpcProcess.start(
                [
                    "/usr/bin/pi",
                    "--mode",
                    "rpc",
                    "--no-session",
                    "--offline",
                    "--approve",
                    "--no-builtin-tools",
                    "--tools",
                    "send_file,send_image",
                    "--extension",
                    str(extension),
                    "--provider",
                    "ollama",
                    "--model",
                    "qwen3.8-orcarouter:latest",
                    "--thinking",
                    "medium",
                ],
                cwd=Path.home(),
                env=broker.child_environment(),
                event_sink=record_event,
            )
            broker.bind_child(process.child_pid)
            try:
                state = await process.request({"type": "get_state"}, 30)
                if not state.success:
                    raise RuntimeError("Pi failed to load artifact extension")
                accepted = await _run_tool_probe(
                    process,
                    events,
                    path=source,
                    expected_accepted=True,
                )
                rejected = await _run_tool_probe(
                    process,
                    events,
                    path=Path("/etc/passwd"),
                    expected_accepted=False,
                )
            finally:
                await process.close()
            print(
                json.dumps(
                    {
                        "extension_loaded": True,
                        "tools_executed": 2,
                        "accepted_receipts": len(broker.receipts()),
                        "rejection_reason": rejected.get("reason"),
                        "telegram_contacted": False,
                    },
                    separators=(",", ":"),
                )
            )
        finally:
            await broker.close()


async def local_text() -> None:
    events: asyncio.Queue[RpcEvent] = asyncio.Queue()

    async def record_event(event: RpcEvent) -> None:
        await events.put(event)

    process = await RpcProcess.start(
        [
            "/usr/bin/pi",
            "--mode",
            "rpc",
            "--no-session",
            "--offline",
            "--approve",
            "--no-tools",
            "--provider",
            "ollama",
            "--model",
            "qwen3.8-orcarouter:latest",
            "--thinking",
            "medium",
        ],
        cwd=Path.home(),
        env={},
        event_sink=record_event,
    )
    try:
        response = await process.request(
            {"type": "prompt", "message": "Reply with exactly PI_OK and nothing else."},
            30,
        )
        if not response.success or not isinstance(response.data, Mapping):
            raise RuntimeError("Pi rejected the local text probe")
        if response.data.get("disposition") not in {"started", "queued"}:
            raise RuntimeError("Pi did not start the local text probe")
        async with asyncio.timeout(5 * 60):
            while (await events.get()).source_type != "agent_settled":
                pass
        final = await process.request({"type": "get_last_assistant_text"}, 30)
        if not final.success or not isinstance(final.data, Mapping):
            raise RuntimeError("Pi returned no local text result")
        text = final.data.get("text")
        if not isinstance(text, str) or text.strip() != "PI_OK":
            raise RuntimeError("Pi local text result did not match PI_OK")
        print(
            json.dumps(
                {
                    "provider": "ollama",
                    "model": "qwen3.8-orcarouter:latest",
                    "result": "PI_OK",
                    "settled": True,
                    "telegram_contacted": False,
                },
                separators=(",", ":"),
            )
        )
    finally:
        await process.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", action="store_true")
    parser.add_argument("--artifact-extension-only", action="store_true")
    parser.add_argument("--local-text", action="store_true")
    arguments = parser.parse_args()
    selected = sum(
        (arguments.metadata, arguments.artifact_extension_only, arguments.local_text)
    )
    if selected != 1:
        parser.error("select one explicit probe")
    if arguments.metadata:
        selected_probe = metadata()
    elif arguments.local_text:
        selected_probe = local_text()
    else:
        selected_probe = artifact_extension_only()
    asyncio.run(selected_probe)


if __name__ == "__main__":
    main()
