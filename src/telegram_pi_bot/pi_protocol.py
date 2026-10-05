from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias

from telegram_pi_bot.model import RuntimeEventKind, UiRequest, UiResponse


MAX_STDOUT_FRAME_BYTES = 16 * 1024 * 1024
MAX_COMMAND_BYTES = 80 * 1024 * 1024
MAX_STDERR_BYTES = 32 * 1024
SHUTDOWN_TIMEOUT_SECONDS = 5.0
TERMINATE_TIMEOUT_SECONDS = 2.0

JsonValue: TypeAlias = (
    None | bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)


class RpcError(RuntimeError):
    """Base class for bounded Pi RPC failures."""


class RpcStartError(RpcError):
    pass


class RpcTimeout(RpcError):
    pass


class RpcProtocolError(RpcError):
    pass


class RpcClosed(RpcError):
    pass


@dataclass(frozen=True, slots=True)
class RpcResponse:
    request_id: str
    command: str
    success: bool
    data: JsonValue = None
    error: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class RpcEvent:
    kind: RuntimeEventKind
    source_type: str
    text: str | None = None
    ui_request: UiRequest | None = None
    payload: Mapping[str, JsonValue] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))


RpcEventSink: TypeAlias = Callable[[RpcEvent], Awaitable[None]]


@dataclass(slots=True)
class _PendingRequest:
    command: str
    future: asyncio.Future[RpcResponse]


class RpcProcess:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        event_sink: RpcEventSink,
        secret_values: tuple[str, ...],
    ) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RpcStartError("RPC child pipes are unavailable")
        self._process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._stderr = process.stderr
        self._event_sink = event_sink
        self._secret_values = secret_values
        self._pending: dict[str, _PendingRequest] = {}
        self._abandoned_ids: set[str] = set()
        self._next_request_id = 1
        self._stderr_ring = bytearray()
        self._write_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._terminal_error: RpcError | None = None
        self._terminal_waiter: asyncio.Future[RpcError] = (
            asyncio.get_running_loop().create_future()
        )
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    @classmethod
    async def start(
        cls,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        event_sink: RpcEventSink,
    ) -> RpcProcess:
        if not argv or any(not isinstance(part, str) or not part for part in argv):
            raise RpcStartError("RPC argv must contain non-empty strings")
        if not isinstance(cwd, Path) or not cwd.is_absolute():
            raise RpcStartError("RPC cwd must be an absolute path")
        if not callable(event_sink):
            raise RpcStartError("RPC event sink must be callable")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()):
            raise RpcStartError("RPC environment keys and values must be strings")

        child_env = os.environ.copy()
        child_env.update(env)
        secret_values = tuple(
            value
            for key, value in child_env.items()
            if value and _SECRET_ENV_NAME.search(key)
        )
        # The transport credential must never reach Pi or its tool subprocesses.
        child_env.pop("TELEGRAM_BOT_TOKEN", None)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=child_env,
            )
        except (OSError, ValueError) as error:
            raise RpcStartError("RPC child failed to start") from error

        instance = cls(process, event_sink, secret_values)
        instance._stdout_task = asyncio.create_task(
            instance._read_stdout(), name="pi-rpc-stdout"
        )
        instance._stderr_task = asyncio.create_task(
            instance._read_stderr(), name="pi-rpc-stderr"
        )
        return instance

    @property
    def child_pid(self) -> int:
        return self._process.pid

    async def request(
        self, record: Mapping[str, Any], timeout: float
    ) -> RpcResponse:
        if isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("RPC timeout must be positive")
        command = record.get("type")
        if not isinstance(command, str) or not command or command in {
            "response",
            "extension_ui_response",
        }:
            raise ValueError("RPC command requires a valid type")
        if "id" in record:
            raise ValueError("RPC request IDs are owned by RpcProcess")
        self._raise_if_unavailable()

        request_id = f"req-{self._next_request_id}"
        self._next_request_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[RpcResponse] = loop.create_future()
        self._pending[request_id] = _PendingRequest(command, future)
        outbound = dict(record)
        outbound["id"] = request_id
        try:
            async with asyncio.timeout(timeout):
                await self._write_record(outbound)
                return await asyncio.shield(future)
        except TimeoutError as error:
            self._abandon(request_id, future)
            raise RpcTimeout(f"RPC command timed out: {command}") from error
        except asyncio.CancelledError:
            self._abandon(request_id, future)
            raise
        except RpcError:
            self._abandon(request_id, future)
            raise

    async def cancel_dialog(self, request_id: str) -> None:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("dialog request ID must be non-empty")
        self._raise_if_unavailable()
        try:
            async with asyncio.timeout(SHUTDOWN_TIMEOUT_SECONDS):
                await self._write_record(
                    {
                        "type": "extension_ui_response",
                        "id": request_id,
                        "cancelled": True,
                    }
                )
        except TimeoutError as error:
            raise RpcTimeout("RPC dialog cancellation timed out") from error

    async def answer_dialog(
        self, request_id: str, method: str, response: UiResponse
    ) -> None:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("dialog request ID must be non-empty")
        if method == "select" and isinstance(response.value, str):
            record: dict[str, Any] = {
                "type": "extension_ui_response",
                "id": request_id,
                "value": response.value,
            }
        elif method == "confirm" and type(response.value) is bool:
            record = {
                "type": "extension_ui_response",
                "id": request_id,
                "confirmed": response.value,
            }
        else:
            raise ValueError("dialog response does not match request method")
        self._raise_if_unavailable()
        try:
            async with asyncio.timeout(SHUTDOWN_TIMEOUT_SECONDS):
                await self._write_record(record)
        except TimeoutError as error:
            raise RpcTimeout("RPC dialog response timed out") from error

    async def wait_for_terminal(self) -> RpcError:
        return await asyncio.shield(self._terminal_waiter)

    def redacted_diagnostic(self) -> str:
        text = bytes(self._stderr_ring).decode("utf-8", errors="replace")
        for secret in sorted(self._secret_values, key=len, reverse=True):
            if len(secret) >= 4:
                text = text.replace(secret, "[REDACTED]")
        text = _AUTHORIZATION.sub(r"\1[REDACTED]", text)
        text = _TOKENISH.sub("[REDACTED]", text)
        text = _truncate_utf8(text, MAX_STDERR_BYTES)
        return text

    async def close(self) -> None:
        async with self._shutdown_lock:
            if self._closed:
                return
            self._closing = True
            self._fail_pending(RpcClosed("RPC process closed"))
            if not self._stdin.is_closing():
                self._stdin.close()
                try:
                    await self._stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                await self._wait_for_exit(SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                await self._terminate_owned_child()
            await self._finish_reader_tasks()
            self._closed = True
            self._notify_terminal(RpcClosed("RPC process closed"))

    async def terminate(self) -> None:
        async with self._shutdown_lock:
            if self._closed:
                return
            self._closing = True
            self._fail_pending(RpcClosed("RPC process terminated"))
            await self._terminate_owned_child()
            if not self._stdin.is_closing():
                self._stdin.close()
            await self._finish_reader_tasks()
            self._closed = True
            self._notify_terminal(RpcClosed("RPC process terminated"))

    async def _write_record(self, record: Mapping[str, Any]) -> None:
        try:
            encoded = json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as error:
            raise RpcProtocolError("RPC command is not valid JSON") from error
        if len(encoded) > MAX_COMMAND_BYTES:
            raise RpcProtocolError("RPC command exceeds the framing limit")
        async with self._write_lock:
            self._raise_if_unavailable()
            try:
                self._stdin.write(encoded)
                await self._stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                closed = RpcClosed("RPC stdin closed unexpectedly")
                self._set_terminal_error(closed)
                raise closed from error

    async def _read_stdout(self) -> None:
        buffer = bytearray()
        try:
            while chunk := await self._stdout.read(64 * 1024):
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        if len(buffer) >= MAX_STDOUT_FRAME_BYTES:
                            raise RpcProtocolError(
                                "RPC stdout frame exceeds the framing limit"
                            )
                        break
                    line = bytes(buffer[:newline])
                    del buffer[: newline + 1]
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    if len(line) > MAX_STDOUT_FRAME_BYTES:
                        raise RpcProtocolError(
                            "RPC stdout frame exceeds the framing limit"
                        )
                    await self._route_line(line)
            if buffer and not self._closing:
                raise RpcProtocolError("RPC stdout ended without LF framing")
            if not self._closing and self._terminal_error is None:
                self._set_terminal_error(RpcClosed("RPC stdout closed unexpectedly"))
        except asyncio.CancelledError:
            raise
        except RpcError as error:
            self._set_terminal_error(error)
            if self._process.returncode is None:
                self._process.terminate()
        except Exception as error:
            protocol_error = RpcProtocolError("RPC event handling failed")
            self._set_terminal_error(protocol_error)
            if self._process.returncode is None:
                self._process.terminate()
            protocol_error.__cause__ = error

    async def _route_line(self, line: bytes) -> None:
        if not line:
            raise RpcProtocolError("RPC stdout contained an empty record")
        try:
            decoded = line.decode("utf-8", errors="strict")
            raw = json.loads(decoded, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise RpcProtocolError("RPC stdout contained invalid JSON") from error
        if not isinstance(raw, dict):
            raise RpcProtocolError("RPC record must be an object")
        record_type = raw.get("type")
        if not isinstance(record_type, str) or not record_type:
            raise RpcProtocolError("RPC record type is missing")
        if record_type == "response":
            self._route_response(raw)
            return
        await self._event_sink(_parse_event(raw))

    def _route_response(self, raw: Mapping[str, Any]) -> None:
        request_id = raw.get("id")
        command = raw.get("command")
        success = raw.get("success")
        if (
            not isinstance(request_id, str)
            or not isinstance(command, str)
            or type(success) is not bool
        ):
            raise RpcProtocolError("RPC response shape is invalid")
        pending = self._pending.get(request_id)
        if pending is None:
            if request_id in self._abandoned_ids:
                self._abandoned_ids.discard(request_id)
                return
            raise RpcProtocolError("RPC response ID is unknown")
        if pending.command != command:
            raise RpcProtocolError("RPC response command does not match request")
        error = raw.get("error")
        if error is not None and not isinstance(error, str):
            raise RpcProtocolError("RPC response error must be text")
        response = RpcResponse(
            request_id=request_id,
            command=command,
            success=success,
            data=_freeze_json(raw.get("data")),
            error=error,
        )
        self._pending.pop(request_id, None)
        if not pending.future.done():
            pending.future.set_result(response)

    async def _read_stderr(self) -> None:
        try:
            while chunk := await self._stderr.read(8 * 1024):
                self._stderr_ring.extend(chunk)
                overflow = len(self._stderr_ring) - MAX_STDERR_BYTES
                if overflow > 0:
                    del self._stderr_ring[:overflow]
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _raise_if_unavailable(self) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._closing or self._closed or self._process.returncode is not None:
            raise RpcClosed("RPC process is not available")

    def _abandon(
        self, request_id: str, future: asyncio.Future[RpcResponse]
    ) -> None:
        self._pending.pop(request_id, None)
        if not future.done():
            future.cancel()
        self._abandoned_ids.add(request_id)
        while len(self._abandoned_ids) > 1024:
            self._abandoned_ids.pop()

    def _set_terminal_error(self, error: RpcError) -> None:
        if self._terminal_error is None:
            self._terminal_error = error
            self._notify_terminal(error)
        self._fail_pending(self._terminal_error)

    def _notify_terminal(self, error: RpcError) -> None:
        if not self._terminal_waiter.done():
            self._terminal_waiter.set_result(error)

    def _fail_pending(self, error: RpcError) -> None:
        pending = tuple(self._pending.values())
        self._pending.clear()
        for item in pending:
            if not item.future.done():
                item.future.set_exception(error)

    async def _terminate_owned_child(self) -> None:
        try:
            try:
                if self._process.returncode is not None:
                    return
                self._process.terminate()
            except ProcessLookupError:
                await self._wait_for_exit(TERMINATE_TIMEOUT_SECONDS)
                return
            try:
                await self._wait_for_exit(TERMINATE_TIMEOUT_SECONDS)
            except TimeoutError:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
                await self._wait_for_exit(TERMINATE_TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise RpcClosed("RPC child did not terminate") from error

    async def _wait_for_exit(self, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self._process.returncode is None:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            await asyncio.sleep(0.01)

    async def _finish_reader_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in (self._stdout_task, self._stderr_task)
            if task is not None and task is not current
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _parse_event(raw: Mapping[str, Any]) -> RpcEvent:
    record_type = raw["type"]
    if record_type == "extension_ui_request":
        request_id = raw.get("id")
        method = raw.get("method")
        if not isinstance(request_id, str) or not isinstance(method, str):
            raise RpcProtocolError("extension UI request shape is invalid")
        title = raw.get("title")
        if title is None:
            title = raw.get("message", method)
        if not isinstance(title, str):
            raise RpcProtocolError("extension UI title must be text")
        options = raw.get("options", ())
        if not isinstance(options, (list, tuple)) or any(
            not isinstance(option, str) for option in options
        ):
            raise RpcProtocolError("extension UI options must be text")
        timeout_ms = raw.get("timeout")
        if timeout_ms is not None and (
            type(timeout_ms) is not int or timeout_ms <= 0
        ):
            raise RpcProtocolError("extension UI timeout must be positive")
        return RpcEvent(
            kind=RuntimeEventKind.UI_REQUEST,
            source_type=record_type,
            text=title,
            ui_request=UiRequest(
                request_id, method, title, tuple(options), timeout_ms
            ),
            payload=_freeze_mapping(raw),
        )

    source_type = raw.get("event") if record_type == "event" else record_type
    if not isinstance(source_type, str) or not source_type:
        raise RpcProtocolError("RPC event shape is invalid")
    text = raw.get("text")
    kind = RuntimeEventKind.PROGRESS
    if record_type == "message_update":
        update = raw.get("assistantMessageEvent")
        if isinstance(update, dict) and update.get("type") == "text_delta":
            delta = update.get("delta")
            if isinstance(delta, str):
                text = delta
                kind = RuntimeEventKind.ASSISTANT_TEXT
    elif source_type == "agent_settled":
        kind = RuntimeEventKind.SETTLED
    elif source_type.startswith("tool_execution_"):
        kind = RuntimeEventKind.TOOL_ACTIVITY
    elif "warning" in source_type or "error" in source_type:
        kind = RuntimeEventKind.WARNING
    if text is not None and not isinstance(text, str):
        raise RpcProtocolError("RPC event text must be text")
    return RpcEvent(
        kind=kind,
        source_type=source_type,
        text=text,
        payload=_freeze_mapping(raw),
    )


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, JsonValue]:
    frozen: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RpcProtocolError("RPC object keys must be text")
        frozen[key] = _freeze_json(item)
    return MappingProxyType(frozen)


def _freeze_json(value: Any) -> JsonValue:
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    raise RpcProtocolError("RPC value is not valid JSON data")


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value
    return encoded[-limit:].decode("utf-8", errors="ignore")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


_SECRET_ENV_NAME = re.compile(r"(?i)(token|secret|passw|api[_-]?key|authorization)")
_AUTHORIZATION = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s]+"
)
_TOKENISH = re.compile(
    r"(?i)\b(?:\d{6,}:[A-Za-z0-9_-]{20,}|[A-Za-z0-9_.-]*token[A-Za-z0-9_.-]*)\b"
)
