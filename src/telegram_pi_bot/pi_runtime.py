"""Typed metadata and native-session boundary for the installed Pi agent."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from telegram_pi_bot.artifact_ipc import (
    ArtifactBroker,
    ArtifactPolicy,
    ArtifactPolicyLike,
)
from telegram_pi_bot.model import (
    ArtifactReceipt,
    CompactionResult,
    ModelRef,
    NativeSession,
    NativeSessionRef,
    PendingSession,
    RuntimeEvent,
    RuntimeEventKind,
    RuntimeSnapshot,
    SessionConfig,
    SessionConfigChange,
    SessionRef,
    SkillRef,
    TurnContent,
    TurnRequest,
    TurnResult,
    TurnStatus,
    UiRequest,
    UiResponse,
)
from telegram_pi_bot.pi_protocol import (
    JsonValue,
    RpcError,
    RpcEvent,
    RpcProcess,
    RpcProtocolError,
    RpcResponse,
)


SESSION_HEADER_LIMIT = 64 * 1024
METADATA_TIMEOUT_SECONDS = 30.0
DEFAULT_THINKING = "medium"


class RuntimeSettings(Protocol):
    cwd: Path
    pi_cli: Path
    sessions_dir: Path
    default_provider: str
    default_model_id: str
    default_thinking: str
    agy_provider: str
    agy_model_id: str
    agy_thinking: str


class RpcClient(Protocol):
    @property
    def child_pid(self) -> int: ...

    async def request(self, record: Mapping[str, Any], timeout: float) -> RpcResponse: ...

    async def close(self) -> None: ...

    async def terminate(self) -> None: ...

    async def cancel_dialog(self, request_id: str) -> None: ...

    async def answer_dialog(
        self, request_id: str, method: str, response: UiResponse
    ) -> None: ...

    async def wait_for_terminal(self) -> RpcError: ...


class ArtifactBrokerLike(Protocol):
    socket_path: Path
    capability: str

    def child_environment(self) -> Mapping[str, str]: ...

    def bind_child(self, pid: int) -> None: ...

    def receipts(self) -> tuple[ArtifactReceipt, ...]: ...

    async def close(self) -> None: ...


RpcFactory = Callable[
    [Sequence[str], Path, Mapping[str, str], Callable[[RpcEvent], Awaitable[None]]],
    Awaitable[RpcClient],
]
ArtifactBrokerFactory = Callable[
    [ArtifactPolicyLike, str], Awaitable[ArtifactBrokerLike]
]
RuntimeEventSink: TypeAlias = Callable[[RuntimeEvent], Awaitable[None]]
ImageLoader: TypeAlias = Callable[[Path], tuple[str, bytes]]


class ActiveTurn(Protocol):
    @property
    def accepted(self) -> bool: ...

    async def steer(self, content: TurnContent) -> None: ...

    async def answer_ui(self, request_id: str, response: UiResponse) -> None: ...

    async def cancel_ui(self, request_id: str) -> None: ...

    async def abort(self) -> None: ...

    async def wait(self) -> TurnResult: ...

    async def close(self) -> None: ...


class PiRuntimeError(RpcError):
    """A bounded runtime error that is safe to surface without provider details."""


class PiRuntime:
    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        rpc_factory: RpcFactory = RpcProcess.start,
        sessions_dir: Path | None = None,
        artifact_policy: ArtifactPolicyLike | None = None,
        artifact_broker_factory: ArtifactBrokerFactory = ArtifactBroker.start,
        artifact_extension_path: Path | None = None,
        image_loader: ImageLoader | None = None,
    ) -> None:
        self._settings = settings
        self._rpc_factory = rpc_factory
        self._sessions_dir = sessions_dir or settings.sessions_dir
        if artifact_policy is None and hasattr(settings, "state_dir"):
            artifact_policy = ArtifactPolicy.from_config(settings)
        self._artifact_policy = artifact_policy
        self._artifact_broker_factory = artifact_broker_factory
        self._artifact_extension_path = artifact_extension_path or (
            Path(__file__).parent / "extensions" / "telegram_artifacts.ts"
        )
        self._image_loader = image_loader

    async def inspect(self, session: NativeSessionRef | None, *, config: SessionConfig | None = None) -> RuntimeSnapshot:
        if session is not None and config is not None:
            raise ValueError("native session settings are owned by Pi")
        path = self._session_path(session) if session is not None else None
        process = await self._start(path, model=config.model if config else None, thinking=config.thinking if config else None)
        try:
            state = await self._mapping_command(process, "get_state")
            models_data = await self._mapping_command(process, "get_available_models")
            levels_data = await self._mapping_command(
                process, "get_available_thinking_levels"
            )
            commands_data = await self._mapping_command(process, "get_commands")
            stats = await self._mapping_command(process, "get_session_stats")
            models = self._allowed_models(models_data.get("models"))
            levels = _text_tuple(levels_data.get("levels"), "thinking levels")
            skills = _parse_skills(commands_data.get("commands"))
            selected_model = _parse_state_model(state.get("model"))
            thinking = _optional_text(state.get("thinkingLevel"), "thinking level")
            session_id = _optional_text(state.get("sessionId"), "session ID")
            session_name = _optional_text(state.get("sessionName"), "session name")
            readiness = {
                provider: True for provider in dict.fromkeys(item.provider for item in models)
            }
            return RuntimeSnapshot(
                models=models,
                thinking_levels=levels,
                skills=skills,
                session_stats=_flatten_stats(stats),
                provider_readiness=readiness,
                selected_model=selected_model,
                thinking=thinking,
                session_id=session_id,
                session_name=session_name,
            )
        finally:
            await process.close()

    async def list_sessions(self, limit: int | None) -> list[NativeSession]:
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 50):
            raise ValueError("session limit must be between 1 and 50")
        sessions = self._discover_sessions()
        sessions.sort(
            key=lambda item: (item.updated_at_ms, item.created_at_ms, item.ref.id),
            reverse=True,
        )
        return sessions[:limit]

    def new_pending_session(self, name: str | None = None) -> PendingSession:
        if name is not None and (not name or name != name.strip()):
            raise ValueError("session name must be non-empty and trimmed")
        model = ModelRef(self._settings.default_provider, self._settings.default_model_id)
        return PendingSession(
            ref=SessionRef("pending", str(uuid.uuid4())),
            name=name,
            config=SessionConfig(model, self._settings.default_thinking),
            created_at_ms=time.time_ns() // 1_000_000,
        )

    async def configure_session(
        self, session: NativeSessionRef, changes: SessionConfigChange
    ) -> RuntimeSnapshot:
        path = self._session_path(session)
        current = await self.inspect(session)
        current_model = current.selected_model
        if current_model is None:
            raise PiRuntimeError("Pi session has no selected model")

        desired_model = self._catalog_model(changes.model or current_model, current.models)
        desired_thinking = changes.thinking or current.thinking or DEFAULT_THINKING
        if self._is_agy(desired_model) and changes.model is not None:
            desired_thinking = self._settings.agy_thinking

        if _same_model(desired_model, current_model):
            valid_levels = current.thinking_levels
        else:
            valid_levels = await self._thinking_levels(desired_model, desired_thinking)
        if desired_thinking not in valid_levels:
            raise ValueError("thinking level is not supported by the selected model")

        process = await self._start(
            path,
            model=desired_model,
            thinking=desired_thinking,
        )
        try:
            if changes.model is not None:
                await self._success_command(
                    process,
                    {
                        "type": "set_model",
                        "provider": desired_model.provider,
                        "modelId": desired_model.model_id,
                    },
                )
            if changes.thinking is not None or (
                changes.model is not None and self._is_agy(desired_model)
            ):
                await self._success_command(
                    process,
                    {"type": "set_thinking_level", "level": desired_thinking},
                )
            if changes.name is not None:
                await self._success_command(
                    process, {"type": "set_session_name", "name": changes.name}
                )
            state = await self._mapping_command(process, "get_state")
            actual_model = _parse_state_model(state.get("model"))
            actual_thinking = _optional_text(state.get("thinkingLevel"), "thinking level")
            if actual_model is None or not _same_model(actual_model, desired_model):
                raise PiRuntimeError("Pi did not apply the requested model")
            if actual_thinking != desired_thinking:
                raise PiRuntimeError("Pi did not apply the requested thinking level")
            return replace(
                current,
                thinking_levels=valid_levels,
                selected_model=actual_model,
                thinking=actual_thinking,
                session_id=_optional_text(state.get("sessionId"), "session ID"),
                session_name=_optional_text(state.get("sessionName"), "session name"),
            )
        finally:
            await process.close()

    async def compact(
        self, session: NativeSessionRef, instructions: str | None
    ) -> CompactionResult:
        if instructions is not None and (
            not instructions.strip() or len(instructions.encode("utf-8")) > 8 * 1024
        ):
            raise ValueError("compaction instructions must be 1 through 8192 bytes")
        path = self._session_path(session)
        process = await self._start(path)
        try:
            command: dict[str, Any] = {"type": "compact"}
            if instructions is not None:
                command["customInstructions"] = instructions
            response = await self._request(process, command, self._turn_timeout())
            data = _response_mapping(response)
            before = _nonnegative_int(data.get("tokensBefore"), "tokens before")
            after = _nonnegative_int(
                data.get("estimatedTokensAfter"), "estimated tokens after"
            )
            return CompactionResult(True, before, after)
        finally:
            await process.close()

    async def resolve_skill(self, name: str) -> SkillRef:
        if not name or name != name.strip():
            raise ValueError("skill name must be non-empty and trimmed")
        snapshot = await self.inspect(None)
        matches = [skill for skill in snapshot.skills if skill.name == name]
        if len(matches) != 1:
            raise LookupError("skill name is not an exact live match")
        return matches[0]

    async def start_turn(
        self, request: TurnRequest, events: RuntimeEventSink
    ) -> ActiveTurn:
        if not callable(events):
            raise ValueError("runtime event sink must be callable")
        argv = self._turn_argv(request.session)
        turn_timeout = self._turn_timeout()
        ui_timeout = self._ui_timeout()
        bridge = _EventBridge()
        broker: ArtifactBrokerLike | None = None
        child_environment: Mapping[str, str] = {}
        if self._artifact_policy is not None:
            if not self._artifact_extension_path.is_file():
                return _TerminalTurn(
                    TurnResult(
                        TurnStatus.REJECTED,
                        text="Pi artifact tools are unavailable.",
                    )
                )
            try:
                broker = await self._artifact_broker_factory(
                    self._artifact_policy, uuid.uuid4().hex
                )
            except (OSError, ValueError):
                return _TerminalTurn(
                    TurnResult(
                        TurnStatus.REJECTED,
                        text="Pi artifact tools could not start safely.",
                    )
                )
            argv.extend(("--extension", str(self._artifact_extension_path)))
            child_environment = broker.child_environment()
        try:
            process = await self._rpc_factory(
                argv, self._settings.cwd, child_environment, bridge.receive
            )
        except RpcError:
            if broker is not None:
                await broker.close()
            return _TerminalTurn(
                TurnResult(
                    TurnStatus.REJECTED,
                    text="Pi could not start the turn.",
                )
            )
        if broker is not None:
            try:
                broker.bind_child(process.child_pid)
            except ValueError:
                try:
                    await process.terminate()
                except RpcError:
                    pass
                await broker.close()
                return _TerminalTurn(
                    TurnResult(
                        TurnStatus.REJECTED,
                        text="Pi artifact tools could not bind safely.",
                    )
                )
        active = _ManagedTurn(
            runtime=self,
            process=process,
            request=request,
            events=events,
            turn_timeout=turn_timeout,
            ui_timeout=ui_timeout,
            artifact_broker=broker,
        )
        try:
            await bridge.attach(active.receive)
            await active.start()
        except BaseException:
            try:
                await process.terminate()
            except RpcError:
                pass
            if broker is not None:
                await broker.close()
            raise
        return active

    def _turn_argv(self, session: PendingSession | NativeSession) -> list[str]:
        argv = [
            str(self._settings.pi_cli),
            "--mode",
            "rpc",
            "--offline",
            "--approve",
        ]
        if isinstance(session, PendingSession):
            argv.extend(("--session-id", session.ref.id))
            if session.name is not None:
                argv.extend(("--name", session.name))
            argv.extend(
                (
                    "--provider",
                    session.config.model.provider,
                    "--model",
                    session.config.model.model_id,
                    "--thinking",
                    session.config.thinking,
                )
            )
            return argv
        path = self._session_path(session.ref)
        argv.extend(("--session", str(path)))
        return argv

    async def _thinking_levels(
        self, model: ModelRef, thinking: str
    ) -> tuple[str, ...]:
        process = await self._start(None, model=model, thinking=thinking)
        try:
            data = await self._mapping_command(process, "get_available_thinking_levels")
            return _text_tuple(data.get("levels"), "thinking levels")
        finally:
            await process.close()

    async def _start(
        self,
        session_path: Path | None,
        *,
        model: ModelRef | None = None,
        thinking: str | None = None,
    ) -> RpcClient:
        argv = [
            str(self._settings.pi_cli),
            "--mode",
            "rpc",
            "--offline",
            "--approve",
        ]
        if session_path is None:
            argv.append("--no-session")
        else:
            argv.extend(("--session", str(session_path)))
        selected = model
        if selected is None and session_path is None:
            selected = ModelRef(
                self._settings.default_provider, self._settings.default_model_id
            )
        if selected is not None:
            argv.extend(("--provider", selected.provider, "--model", selected.model_id))
        selected_thinking = thinking
        if selected_thinking is None and session_path is None:
            selected_thinking = self._settings.default_thinking
        if selected_thinking is not None:
            argv.extend(("--thinking", selected_thinking))
        return await self._rpc_factory(argv, self._settings.cwd, {}, _discard_event)

    async def _mapping_command(
        self, process: RpcClient, command: str
    ) -> Mapping[str, JsonValue]:
        response = await self._request(
            process, {"type": command}, METADATA_TIMEOUT_SECONDS
        )
        return _response_mapping(response)

    async def _success_command(
        self, process: RpcClient, command: Mapping[str, Any]
    ) -> JsonValue:
        return (await self._request(process, command, METADATA_TIMEOUT_SECONDS)).data

    async def _request(
        self, process: RpcClient, command: Mapping[str, Any], timeout: float
    ) -> RpcResponse:
        response = await process.request(command, timeout)
        if not response.success:
            command_name = command.get("type")
            raise PiRuntimeError(f"Pi RPC command failed: {command_name}")
        return response

    def _allowed_models(self, raw: JsonValue) -> tuple[ModelRef, ...]:
        items = _object_sequence(raw, "model catalog")
        allowed: list[ModelRef] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            parsed = _parse_catalog_model(item)
            if parsed.provider == "ollama" or self._is_agy(parsed):
                key = (parsed.provider, parsed.model_id)
                if key in seen:
                    raise PiRuntimeError("Pi model catalog contains a duplicate")
                seen.add(key)
                allowed.append(parsed)
        return tuple(allowed)

    def _catalog_model(
        self, requested: ModelRef, available: tuple[ModelRef, ...]
    ) -> ModelRef:
        matches = [item for item in available if _same_model(item, requested)]
        if len(matches) != 1:
            raise LookupError("model is not allowed by the live catalog")
        return matches[0]

    def _is_agy(self, model: ModelRef) -> bool:
        return (
            model.provider == self._settings.agy_provider
            and model.model_id == self._settings.agy_model_id
        )

    def _session_path(self, session: NativeSessionRef) -> Path:
        matches = [item for item in self._discover_headers() if item[0].ref.id == session.id]
        if len(matches) != 1:
            raise LookupError("native session is unavailable or ambiguous")
        return matches[0][1]

    def _discover_sessions(self) -> list[NativeSession]:
        headers = self._discover_headers()
        counts: dict[str, int] = {}
        for session, _path in headers:
            counts[session.ref.id] = counts.get(session.ref.id, 0) + 1
        return [
            session
            for session, _path in headers
            if counts[session.ref.id] == 1
        ]

    def _discover_headers(self) -> list[tuple[NativeSession, Path]]:
        try:
            entries = tuple(self._sessions_dir.iterdir())
        except FileNotFoundError:
            return []
        except OSError as error:
            raise PiRuntimeError("native session directory is unavailable") from error

        found: list[tuple[NativeSession, Path]] = []
        for path in entries:
            if path.suffix != ".jsonl":
                continue
            parsed = self._read_header(path)
            if parsed is None:
                continue
            found.append((parsed, path))
        return found

    def _read_header(self, path: Path) -> NativeSession | None:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return None
            with os.fdopen(descriptor, "rb", buffering=0, closefd=False) as handle:
                line = handle.readline(SESSION_HEADER_LIMIT + 1)
            if not line.endswith(b"\n") or len(line) > SESSION_HEADER_LIMIT:
                return None
            try:
                header = json.loads(
                    line.decode("utf-8", errors="strict"),
                    parse_constant=_reject_json_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                return None
            if not isinstance(header, dict):
                return None
            if (
                header.get("type") != "session"
                or header.get("version") != 3
                or header.get("cwd") != str(self._settings.cwd)
            ):
                return None
            session_id = header.get("id")
            timestamp = header.get("timestamp")
            if not isinstance(session_id, str) or not isinstance(timestamp, str):
                return None
            try:
                uuid.UUID(session_id)
                created = datetime.fromisoformat(timestamp)
            except ValueError:
                return None
            if created.tzinfo is None:
                return None
            return NativeSession(
                ref=NativeSessionRef(session_id),
                name=None,
                created_at_ms=int(created.timestamp() * 1000),
                updated_at_ms=metadata.st_mtime_ns // 1_000_000,
                config=None,
            )
        finally:
            os.close(descriptor)

    def _turn_timeout(self) -> float:
        value = getattr(self._settings, "turn_timeout_seconds", 6 * 60 * 60)
        if type(value) not in {int, float} or value <= 0:
            raise PiRuntimeError("turn timeout configuration is invalid")
        return float(value)

    def _ui_timeout(self) -> float:
        value = getattr(self._settings, "ui_timeout_seconds", 10 * 60)
        if type(value) not in {int, float} or value <= 0:
            raise PiRuntimeError("UI timeout configuration is invalid")
        return float(value)


class _EventBridge:
    def __init__(self) -> None:
        self._sink: Callable[[RpcEvent], Awaitable[None]] | None = None
        self._pending: list[RpcEvent] = []

    async def receive(self, event: RpcEvent) -> None:
        if self._sink is None:
            self._pending.append(event)
            return
        await self._sink(event)

    async def attach(self, sink: Callable[[RpcEvent], Awaitable[None]]) -> None:
        if self._sink is not None:
            raise RuntimeError("RPC event bridge is already attached")
        self._sink = sink
        pending, self._pending = self._pending, []
        for event in pending:
            await sink(event)


class _TerminalTurn:
    def __init__(self, result: TurnResult) -> None:
        self._result = result

    @property
    def accepted(self) -> bool:
        return self._result.status is not TurnStatus.REJECTED

    async def steer(self, content: TurnContent) -> None:
        raise RuntimeError("turn is already terminal")

    async def answer_ui(self, request_id: str, response: UiResponse) -> None:
        raise LookupError("blocking UI request is no longer active")

    async def cancel_ui(self, request_id: str) -> None:
        return None

    async def abort(self) -> None:
        return None

    async def wait(self) -> TurnResult:
        return self._result

    async def close(self) -> None:
        return None


class _ManagedTurn:
    def __init__(
        self,
        *,
        runtime: PiRuntime,
        process: RpcClient,
        request: TurnRequest,
        events: RuntimeEventSink,
        turn_timeout: float,
        ui_timeout: float,
        artifact_broker: ArtifactBrokerLike | None,
    ) -> None:
        self._runtime = runtime
        self._process = process
        self._request = request
        self._events = events
        self._ui_timeout = ui_timeout
        self._artifact_broker = artifact_broker
        self._deadline = asyncio.get_running_loop().time() + turn_timeout
        self._result: asyncio.Future[TurnResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._accepted = False
        self._disposition: str | None = None
        self._settled_seen = False
        self._saw_failure = False
        self._completed_text: list[str] = []
        self._partial_text: list[str] = []
        self._pending_ui: UiRequest | None = None
        self._ui_timer: asyncio.Task[None] | None = None
        self._deadline_task: asyncio.Task[None] | None = None
        self._terminal_task: asyncio.Task[None] | None = asyncio.create_task(
            self._watch_process(), name="pi-turn-process"
        )
        self._finish_lock = asyncio.Lock()
        self._ui_lock = asyncio.Lock()
        self._abort_lock = asyncio.Lock()

    @property
    def accepted(self) -> bool:
        return self._accepted

    async def start(self) -> None:
        try:
            previous_text = await self._last_assistant_text()
            remaining = self._remaining_time()
            command = await self._content_command("prompt", self._request.content)
            response = await self._process.request(
                command,
                remaining,
            )
        except PiRuntimeError as error:
            await self._finish(TurnStatus.REJECTED, str(error))
            return
        except RpcError:
            await self._finish(
                TurnStatus.REJECTED,
                "Pi rejected the turn before acceptance.",
            )
            return
        if not response.success:
            await self._finish(
                TurnStatus.REJECTED,
                "Pi rejected the turn before acceptance.",
            )
            return
        try:
            data = _response_mapping(response)
            disposition = data.get("disposition")
            if not isinstance(disposition, str):
                raise PiRuntimeError("Pi prompt disposition is invalid")
        except RpcError:
            self._accepted = True
            await self._finish(
                TurnStatus.UNCERTAIN,
                "Pi accepted the turn with an unknown result.",
            )
            return

        self._accepted = True
        self._disposition = disposition
        if disposition == "handled":
            try:
                latest_text = await self._last_assistant_text()
            except RpcError:
                await self._finish(
                    TurnStatus.UNCERTAIN,
                    "Pi handled the input but its result could not be confirmed.",
                )
                return
            text = (
                latest_text
                if latest_text and latest_text != previous_text
                else "Input was handled without assistant text."
            )
            await self._finish(TurnStatus.HANDLED, text)
            return
        if disposition not in {"started", "queued"}:
            await self._finish(
                TurnStatus.UNCERTAIN,
                "Pi accepted the turn with an unknown result.",
            )
            return

        self._deadline_task = asyncio.create_task(
            self._watch_deadline(), name="pi-turn-deadline"
        )
        if self._settled_seen:
            await self._finish_after_settlement()

    async def receive(self, event: RpcEvent) -> None:
        if self._result.done():
            return
        if event.kind is RuntimeEventKind.UI_REQUEST:
            await self._receive_ui(event)
            return

        assistant_text = _assistant_event_text(event)
        if assistant_text is not None:
            if event.source_type == "message_end":
                self._completed_text.append(assistant_text)
                self._partial_text.clear()
            else:
                self._partial_text.append(assistant_text)
            await self._events(
                RuntimeEvent(RuntimeEventKind.ASSISTANT_TEXT, text=assistant_text)
            )
        elif event.kind is RuntimeEventKind.TOOL_ACTIVITY:
            await self._events(
                RuntimeEvent(
                    RuntimeEventKind.TOOL_ACTIVITY,
                    summary=_bounded_text(event.source_type, 160),
                )
            )
        elif event.kind is RuntimeEventKind.WARNING:
            self._saw_failure = True
            await self._events(
                RuntimeEvent(
                    RuntimeEventKind.WARNING,
                    text="Pi reported a runtime warning.",
                )
            )
        elif event.kind is RuntimeEventKind.SETTLED:
            self._settled_seen = True
            await self._events(RuntimeEvent(RuntimeEventKind.SETTLED))
            if self._accepted and self._disposition in {"started", "queued"}:
                await self._finish_after_settlement()
        elif event.text:
            await self._events(
                RuntimeEvent(
                    RuntimeEventKind.PROGRESS,
                    text=_bounded_text(event.text, 500),
                )
            )

        if _event_failed(event):
            self._saw_failure = True

    async def steer(self, content: TurnContent) -> None:
        if not self._accepted or self._result.done():
            raise RuntimeError("turn is not active")
        command = await self._content_command("steer", content)
        response = await self._process.request(
            command,
            min(METADATA_TIMEOUT_SECONDS, self._remaining_time()),
        )
        if not response.success:
            raise PiRuntimeError("Pi RPC command failed: steer")
        data = _response_mapping(response)
        if data.get("disposition") not in {"queued", "handled"}:
            raise PiRuntimeError("Pi steer disposition is invalid")

    async def _content_command(
        self, command: str, content: TurnContent
    ) -> dict[str, Any]:
        record: dict[str, Any] = {"type": command, "message": content.text}
        if not content.attachments:
            return record
        state_response = await self._process.request(
            {"type": "get_state"},
            min(METADATA_TIMEOUT_SECONDS, self._remaining_time()),
        )
        if not state_response.success:
            raise PiRuntimeError("Pi could not confirm image support")
        state = _response_mapping(state_response)
        model = _parse_state_model(state.get("model"))
        if model is None or "image" not in model.capabilities:
            raise PiRuntimeError(
                "The selected model does not accept images. Choose an image-capable model and retry."
            )
        loader = self._runtime._image_loader
        if loader is None:
            raise PiRuntimeError("Validated image loading is unavailable")
        images: list[dict[str, str]] = []
        for value in content.attachments:
            try:
                mime_type, payload = await asyncio.to_thread(loader, Path(value))
            except (OSError, ValueError):
                raise PiRuntimeError("A photo attachment is unavailable or invalid") from None
            images.append(
                {
                    "type": "image",
                    "data": base64.b64encode(payload).decode("ascii"),
                    "mimeType": mime_type,
                }
            )
        record["images"] = images
        return record

    async def answer_ui(self, request_id: str, response: UiResponse) -> None:
        async with self._ui_lock:
            pending = self._pending_ui
            if pending is None or pending.request_id != request_id:
                raise LookupError("blocking UI request is no longer active")
            if pending.kind == "select" and (
                not isinstance(response.value, str)
                or response.value not in pending.options
            ):
                raise ValueError("select response must be one listed option")
            if pending.kind == "confirm" and type(response.value) is not bool:
                raise ValueError("confirm response must be boolean")
            self._pending_ui = None
            self._cancel_ui_timer()
        try:
            await self._process.answer_dialog(
                request_id, pending.kind, response
            )
        except RpcError:
            await self._finish(
                TurnStatus.UNCERTAIN,
                "Pi accepted the turn but the approval response was not confirmed.",
            )
            raise

    async def cancel_ui(self, request_id: str) -> None:
        async with self._ui_lock:
            pending = self._pending_ui
            if pending is None or pending.request_id != request_id:
                return
            self._pending_ui = None
            self._cancel_ui_timer()
        try:
            await self._process.cancel_dialog(request_id)
        except RpcError:
            await self._finish(
                TurnStatus.UNCERTAIN,
                "Pi accepted the turn but approval cancellation was not confirmed.",
            )
            raise

    async def abort(self) -> None:
        async with self._abort_lock:
            if self._result.done():
                return
            try:
                await self._cancel_pending_ui()
                clear = await self._process.request(
                    {"type": "clear_queue"},
                    min(METADATA_TIMEOUT_SECONDS, self._remaining_time()),
                )
                if not clear.success:
                    raise PiRuntimeError("Pi RPC command failed: clear_queue")
                aborted = await self._process.request(
                    {"type": "abort"},
                    min(METADATA_TIMEOUT_SECONDS, self._remaining_time()),
                )
                if not aborted.success:
                    raise PiRuntimeError("Pi RPC command failed: abort")
            except RpcError:
                try:
                    await self._process.terminate()
                except RpcError:
                    pass
                await self._finish(
                    TurnStatus.UNCERTAIN,
                    "Pi queue clearing could not be confirmed; the owned process was stopped.",
                )
                return
            await self._finish(TurnStatus.ABORTED, self._final_text() or None)

    async def wait(self) -> TurnResult:
        return await asyncio.shield(self._result)

    async def close(self) -> None:
        if self._result.done():
            return
        try:
            await self._cancel_pending_ui()
        except RpcError:
            pass
        status = TurnStatus.UNCERTAIN if self._accepted else TurnStatus.REJECTED
        await self._finish(status, "The Pi turn was closed before completion.")

    async def _receive_ui(self, event: RpcEvent) -> None:
        request = event.ui_request
        if request is None:
            raise RpcProtocolError("Pi UI event is missing its typed request")
        if request.kind in {"input", "editor"}:
            await self._process.cancel_dialog(request.request_id)
            await self._events(
                RuntimeEvent(
                    RuntimeEventKind.WARNING,
                    text=f"Unsupported Pi {request.kind} request was cancelled.",
                )
            )
            return
        if request.kind not in {"select", "confirm"}:
            if event.text:
                await self._events(
                    RuntimeEvent(
                        RuntimeEventKind.PROGRESS,
                        text=_bounded_text(event.text, 500),
                    )
                )
            return
        async with self._ui_lock:
            if self._pending_ui is not None:
                await self._process.cancel_dialog(request.request_id)
                raise RpcProtocolError("Pi emitted overlapping blocking UI requests")
            self._pending_ui = request
            timeout = self._ui_timeout
            if request.timeout_ms is not None:
                timeout = min(timeout, request.timeout_ms / 1000)
            self._ui_timer = asyncio.create_task(
                self._expire_ui(request.request_id, timeout),
                name="pi-turn-ui-timeout",
            )
        await self._events(
            RuntimeEvent(RuntimeEventKind.UI_REQUEST, ui_request=request)
        )

    async def _expire_ui(self, request_id: str, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
            async with self._ui_lock:
                if (
                    self._pending_ui is None
                    or self._pending_ui.request_id != request_id
                ):
                    return
                self._pending_ui = None
                self._ui_timer = None
            await self._process.cancel_dialog(request_id)
            await self._events(
                RuntimeEvent(
                    RuntimeEventKind.WARNING,
                    text="Pi approval request expired and was cancelled.",
                )
            )
        except asyncio.CancelledError:
            return
        except RpcError:
            await self._finish(
                TurnStatus.UNCERTAIN,
                "Pi accepted the turn but approval expiry was not confirmed.",
            )

    async def _cancel_pending_ui(self) -> None:
        async with self._ui_lock:
            pending = self._pending_ui
            self._pending_ui = None
            self._cancel_ui_timer()
        if pending is not None:
            await self._process.cancel_dialog(pending.request_id)

    def _cancel_ui_timer(self) -> None:
        if self._ui_timer is not None:
            self._ui_timer.cancel()
            self._ui_timer = None

    async def _watch_deadline(self) -> None:
        try:
            await asyncio.sleep(self._remaining_time())
            try:
                await self._process.terminate()
            except RpcError:
                pass
            await self._finish(
                TurnStatus.UNCERTAIN,
                "Pi accepted the turn but exceeded the six-hour deadline.",
            )
        except asyncio.CancelledError:
            return

    async def _watch_process(self) -> None:
        try:
            await self._process.wait_for_terminal()
            if self._result.done():
                return
            status = TurnStatus.UNCERTAIN if self._accepted else TurnStatus.REJECTED
            await self._finish(
                status,
                "The Pi process ended before the turn completed.",
            )
        except asyncio.CancelledError:
            return

    async def _last_assistant_text(self) -> str | None:
        response = await self._process.request(
            {"type": "get_last_assistant_text"},
            min(METADATA_TIMEOUT_SECONDS, self._remaining_time()),
        )
        if not response.success:
            raise PiRuntimeError("Pi RPC command failed: get_last_assistant_text")
        data = _response_mapping(response)
        text = data.get("text")
        if text is not None and not isinstance(text, str):
            raise PiRuntimeError("Pi last assistant text response is invalid")
        return text

    async def _finish_after_settlement(self) -> None:
        status = TurnStatus.FAILED if self._saw_failure else TurnStatus.COMPLETED
        await self._finish(status, self._final_text() or None)

    async def _finish(self, status: TurnStatus, text: str | None) -> None:
        async with self._finish_lock:
            if self._result.done():
                return
            current = asyncio.current_task()
            for task in (self._deadline_task, self._terminal_task, self._ui_timer):
                if task is not None and task is not current:
                    task.cancel()
            self._deadline_task = None
            self._terminal_task = None
            self._ui_timer = None
            self._pending_ui = None
            try:
                await self._process.close()
            except RpcError:
                if self._accepted and status not in {
                    TurnStatus.ABORTED,
                    TurnStatus.UNCERTAIN,
                }:
                    status = TurnStatus.UNCERTAIN
                    text = "Pi completed but process shutdown could not be confirmed."
            artifacts: tuple[ArtifactReceipt, ...] = ()
            if self._artifact_broker is not None:
                artifacts = self._artifact_broker.receipts()
                try:
                    await self._artifact_broker.close()
                except OSError:
                    if status not in {TurnStatus.ABORTED, TurnStatus.UNCERTAIN}:
                        status = TurnStatus.UNCERTAIN
                        text = "Pi completed but artifact IPC shutdown was uncertain."
            try:
                materialized = self._materialized_session()
            except (OSError, RpcError):
                materialized = None
            self._result.set_result(
                TurnResult(
                    status=status,
                    text=text,
                    artifacts=artifacts,
                    materialized_session=materialized,
                )
            )

    def _materialized_session(self) -> NativeSession | None:
        if (
            not self._accepted
            or self._disposition not in {"started", "queued"}
            or not isinstance(self._request.session, PendingSession)
        ):
            return None
        session_id = self._request.session.ref.id
        matches = [
            session
            for session, _path in self._runtime._discover_headers()
            if session.ref.id == session_id
        ]
        if len(matches) != 1:
            return None
        return replace(
            matches[0],
            name=self._request.session.name,
            config=self._request.session.config,
        )

    def _remaining_time(self) -> float:
        return max(0.001, self._deadline - asyncio.get_running_loop().time())

    def _final_text(self) -> str:
        sections = [*self._completed_text]
        partial = "".join(self._partial_text)
        if partial:
            sections.append(partial)
        return "\n\n".join(section for section in sections if section)


async def _discard_event(_event: RpcEvent) -> None:
    return None


def _response_mapping(response: RpcResponse) -> Mapping[str, JsonValue]:
    if not isinstance(response.data, Mapping):
        raise PiRuntimeError(f"Pi RPC response shape is invalid: {response.command}")
    return response.data


def _object_sequence(raw: JsonValue, label: str) -> tuple[Mapping[str, JsonValue], ...]:
    if not isinstance(raw, (list, tuple)):
        raise PiRuntimeError(f"Pi {label} response is invalid")
    result: list[Mapping[str, JsonValue]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise PiRuntimeError(f"Pi {label} entry is invalid")
        result.append(item)
    return tuple(result)


def _parse_catalog_model(raw: Mapping[str, JsonValue]) -> ModelRef:
    provider = raw.get("provider")
    model_id = raw.get("id")
    if not isinstance(provider, str) or not isinstance(model_id, str):
        raise PiRuntimeError("Pi model entry is invalid")
    capabilities = _text_tuple(raw.get("input", ()), "model input")
    if provider == "ollama":
        location = "ollama_cloud" if model_id.endswith(":cloud") else "local"
    else:
        location = "remote"
    return ModelRef(provider, model_id, location, capabilities)


def _parse_state_model(raw: JsonValue) -> ModelRef | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PiRuntimeError("Pi selected model is invalid")
    provider = raw.get("provider")
    model_id = raw.get("id")
    if not isinstance(provider, str) or not isinstance(model_id, str):
        raise PiRuntimeError("Pi selected model is invalid")
    capabilities = _text_tuple(raw.get("input", ()), "selected model input")
    location = (
        "ollama_cloud"
        if provider == "ollama" and model_id.endswith(":cloud")
        else "local" if provider == "ollama" else "remote"
    )
    return ModelRef(provider, model_id, location, capabilities)


def _parse_skills(raw: JsonValue) -> tuple[SkillRef, ...]:
    commands = _object_sequence(raw, "command catalog")
    result: list[SkillRef] = []
    seen: set[str] = set()
    for command in commands:
        if command.get("source") != "skill":
            continue
        raw_name = command.get("name")
        description = command.get("description", "")
        if (
            not isinstance(raw_name, str)
            or not raw_name.startswith("skill:")
            or not isinstance(description, str)
        ):
            raise PiRuntimeError("Pi skill command entry is invalid")
        name = raw_name.removeprefix("skill:")
        if not name or name in seen:
            raise PiRuntimeError("Pi skill command name is invalid or duplicated")
        seen.add(name)
        result.append(SkillRef(name, description))
    return tuple(result)


def _text_tuple(raw: JsonValue, label: str) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in raw
    ):
        raise PiRuntimeError(f"Pi {label} response is invalid")
    return tuple(raw)


def _optional_text(raw: JsonValue, label: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise PiRuntimeError(f"Pi {label} response is invalid")
    return raw


def _flatten_stats(raw: Mapping[str, JsonValue]) -> dict[str, int | float | str | None]:
    flattened: dict[str, int | float | str | None] = {}

    def visit(prefix: str, value: JsonValue) -> None:
        if value is None or type(value) in {int, float, str}:
            flattened[prefix] = value  # type: ignore[assignment]
            return
        if type(value) is bool:
            flattened[prefix] = str(value).lower()
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                visit(f"{prefix}.{key}" if prefix else key, item)

    for key, value in raw.items():
        visit(key, value)
    return flattened


def _nonnegative_int(raw: JsonValue, label: str) -> int:
    if type(raw) is not int or raw < 0:
        raise PiRuntimeError(f"Pi {label} response is invalid")
    return raw


def _same_model(left: ModelRef, right: ModelRef) -> bool:
    return left.provider == right.provider and left.model_id == right.model_id


def _assistant_event_text(event: RpcEvent) -> str | None:
    if event.kind is RuntimeEventKind.ASSISTANT_TEXT:
        return event.text
    if event.source_type != "message_end":
        return None
    message = event.payload.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return None
    text: list[str] = []
    for block in content:
        if (
            isinstance(block, Mapping)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            text.append(block["text"])
    return "".join(text) or None


def _event_failed(event: RpcEvent) -> bool:
    if "error" in event.source_type:
        return True
    if event.source_type != "message_end":
        return False
    message = event.payload.get("message")
    if not isinstance(message, Mapping):
        return False
    return message.get("stopReason") in {"error", "aborted"}


def _bounded_text(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")
