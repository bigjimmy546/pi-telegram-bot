"""Typed deterministic Pi RPC fixtures for runtime contract tests."""

from __future__ import annotations

import copy
import asyncio
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from telegram_pi_bot.model import (
    ArtifactReceipt,
    NativeSession,
    NativeSessionRef,
    RuntimeEvent,
    RuntimeEventKind,
    SessionConfig,
    ModelRef,
    TurnResult,
    TurnStatus,
    UiRequest,
    UiResponse,
)
from telegram_pi_bot.pi_protocol import RpcClosed, RpcEvent, RpcResponse


TEST_CHAT_ID = 123456789


def empty_state(now_ms: int = 0, **overrides):
    """Build a public empty coordinator state for transition tests."""
    from telegram_pi_bot.model import BotState

    return BotState(
        version=overrides.pop("version", 0),
        chat_id=overrides.pop("chat_id", TEST_CHAT_ID),
        now_ms=now_ms,
        **overrides,
    )


def conversation_action(kind: str, **values):
    """Create an immutable action without coupling tests to coordinator internals."""
    from telegram_pi_bot.model import ConversationAction

    return ConversationAction(kind=kind, **values)


def add_text(text: str, *, source_message_id: int, now_ms: int = 1_000):
    return conversation_action(
        "add_text", text=text, source_message_id=source_message_id, now_ms=now_ms
    )


def add_photo(file_id: str, *, source_message_id: int, now_ms: int = 1_000):
    return conversation_action(
        "add_photo",
        attachment_id=file_id,
        source_message_id=source_message_id,
        now_ms=now_ms,
    )


def send_now(bundle_id: str, *, now_ms: int = 1_000):
    return conversation_action("send_now", bundle_id=bundle_id, now_ms=now_ms)


def steer_current(bundle_id: str, *, now_ms: int = 1_000):
    return conversation_action("steer_current", bundle_id=bundle_id, now_ms=now_ms)


def stop_action(*, now_ms: int = 1_000):
    return conversation_action("stop", now_ms=now_ms)


def state_with_active_turn(turn_id: str = "turn-1", **overrides):
    from telegram_pi_bot.model import TurnRecord

    active = TurnRecord(turn_id, "native-1", 1, "active", True, 0)
    return empty_state(active_turn=active, **overrides)


def state_with_active_and_queued_turn():
    from telegram_pi_bot.coordinator import transition

    queued = transition(
        state_with_active_turn(), add_text("queued", source_message_id=20)
    )
    return queued.state


def effect_kinds(transition_value):
    return [effect.kind for effect in transition_value.effects]


def make_store(root):
    """Construct a control store at a caller-owned temporary database path."""
    from telegram_pi_bot.store import ControlStore

    return ControlStore(root / "control.sqlite3")


@dataclass(frozen=True, slots=True)
class PiModel:
    provider: str
    model_id: str
    capabilities: tuple[str, ...] = ("text",)


@dataclass(frozen=True, slots=True)
class PiCommand:
    name: str
    source: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    cwd: Path = Path("/home/alice")
    pi_cli: Path = Path("/usr/bin/pi")
    sessions_dir: Path = Path("/home/alice/.pi/agent/sessions")
    default_provider: str = "ollama"
    default_model_id: str = "qwen3.8-orcarouter:latest"
    default_thinking: str = "medium"
    agy_provider: str = "antigravity"
    agy_model_id: str = "gemini-3.7-flash"
    agy_thinking: str = "high"
    turn_timeout_seconds: float = 6 * 60 * 60
    ui_timeout_seconds: float = 10 * 60


def model(provider: str, model_id: str, capabilities: Sequence[str] = ("text",)) -> PiModel:
    return PiModel(provider, model_id, tuple(capabilities))


def mixed_provider_models() -> tuple[PiModel, ...]:
    return (
        model("ollama", "qwen3.8-orcarouter:latest"),
        model("ollama", "vision", ("text", "image")),
        model("ollama", "remote:cloud"),
        model("antigravity", "gemini-3.7-flash"),
        model("antigravity", "other"),
        model("openrouter", "hidden"),
    )


def expected_pi_ollama_ids() -> list[str]:
    return ["ollama/qwen3.8-orcarouter:latest", "ollama/vision", "ollama/remote:cloud"]


def skill_command(name: str, description: str = "") -> PiCommand:
    return PiCommand(f"skill:{name}", "skill", description)


def extension_command(name: str, description: str = "") -> PiCommand:
    return PiCommand(name, "extension", description)


def _model_data(value: PiModel) -> dict[str, Any]:
    return {
        "provider": value.provider,
        "id": value.model_id,
        "input": list(value.capabilities),
        "contextWindow": 32768,
        "maxTokens": 8192,
        "cost": {"input": 0, "output": 0},
    }


def _command_data(value: PiCommand) -> dict[str, str]:
    return {"name": value.name, "source": value.source, "description": value.description}


def runtime_with_models(
    models: Sequence[PiModel],
    *,
    global_default: tuple[str, str] = ("openrouter", "global-default"),
    default_model: tuple[str, str] = ("ollama", "qwen3.8-orcarouter:latest"),
    default_thinking: str = "medium",
):
    """Return a PiRuntime and its typed fake process factory."""
    from telegram_pi_bot.pi_runtime import PiRuntime

    responses = {
        "get_state": {"model": {"provider": global_default[0], "id": global_default[1]}, "thinkingLevel": "low"},
        "get_available_models": {"models": [_model_data(item) for item in models]},
        "get_available_thinking_levels": {"levels": ["off", "low", "medium", "high"]},
        "get_commands": {"commands": []},
        "get_session_stats": {"tokens": 7, "compactions": 0, "cost": None},
    }
    factory = FakeRpcFactory(responses)
    settings = RuntimeSettings(
        default_provider=default_model[0],
        default_model_id=default_model[1],
        default_thinking=default_thinking,
    )
    return PiRuntime(settings, rpc_factory=factory), factory


def runtime_with_commands(commands: Sequence[PiCommand]):
    from telegram_pi_bot.pi_runtime import PiRuntime

    responses = {
        "get_state": {"model": {"provider": "ollama", "id": "global-default"}, "thinkingLevel": "medium"},
        "get_available_models": {
            "models": [_model_data(model("ollama", "qwen3.8-orcarouter:latest"))]
        },
        "get_available_thinking_levels": {"levels": ["off", "low", "medium", "high"]},
        "get_commands": {"commands": [_command_data(item) for item in commands]},
        "get_session_stats": {"tokens": 0, "compactions": 0, "cost": None},
    }
    factory = FakeRpcFactory(responses)
    return PiRuntime(RuntimeSettings(), rpc_factory=factory), factory


class FakeRpcProcess:
    def __init__(
        self,
        responses: Mapping[str, Any],
        event_sink,
        command_overrides: Mapping[str, Sequence[RpcResponse]] | None = None,
    ) -> None:
        self._responses = copy.deepcopy(responses)
        self._event_sink = event_sink
        self._overrides = {key: list(items) for key, items in (command_overrides or {}).items()}
        self.commands: list[dict[str, Any]] = []
        self.events: list[RpcEvent] = []
        self.closed = False

    @property
    def child_pid(self) -> int:
        return 10_001

    async def request(self, record: Mapping[str, Any], timeout: float) -> RpcResponse:
        command = record["type"]
        self.commands.append(dict(record))
        override = self._overrides.get(command)
        if override:
            return override.pop(0)
        state = self._responses.get("get_state")
        if isinstance(state, dict):
            if command == "set_model":
                state["model"] = {
                    "provider": record["provider"],
                    "id": record["modelId"],
                }
            elif command == "set_thinking_level":
                state["thinkingLevel"] = record["level"]
            elif command == "set_session_name":
                state["sessionName"] = record["name"]
        data = self._responses.get(command)
        return RpcResponse(f"fake-{len(self.commands)}", command, True, data)

    async def cancel_dialog(self, request_id: str) -> None:
        raise AssertionError("metadata runtime must not cancel dialogs")

    async def close(self) -> None:
        self.closed = True

    async def terminate(self) -> None:
        self.closed = True


class FakeRpcFactory:
    def __init__(
        self,
        responses: Mapping[str, Any],
        *,
        command_overrides: Mapping[str, Sequence[RpcResponse]] | None = None,
    ) -> None:
        self.responses = dict(responses)
        self.command_overrides = command_overrides or {}
        self.processes: list[FakeRpcProcess] = []
        self.argv: list[tuple[str, ...]] = []
        self.cwd: list[Path] = []
        self.env: list[dict[str, str]] = []

    async def __call__(self, argv, cwd, env, event_sink) -> FakeRpcProcess:
        self.argv.append(tuple(argv))
        self.cwd.append(cwd)
        self.env.append(dict(env))
        responses = copy.deepcopy(self.responses)
        state = responses.get("get_state")
        if isinstance(state, dict):
            provider = _argument(argv, "--provider")
            model_id = _argument(argv, "--model")
            thinking = _argument(argv, "--thinking")
            if provider is not None and model_id is not None:
                state["model"] = {"provider": provider, "id": model_id}
            if thinking is not None:
                state["thinkingLevel"] = thinking
        process = FakeRpcProcess(responses, event_sink, self.command_overrides)
        self.processes.append(process)
        return process

    @property
    def prompt_count(self) -> int:
        return sum(
            command["type"] == "prompt"
            for process in self.processes
            for command in process.commands
        )

    @property
    def commands(self) -> list[dict[str, Any]]:
        return [command for process in self.processes for command in process.commands]


def _argument(argv: Sequence[str], flag: str) -> str | None:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def rpc_response(command: str, data: Any = None, *, success: bool = True, error: str | None = None) -> RpcResponse:
    return RpcResponse(f"fixture-{command}", command, success, data, error)


def event(source_type: str, *, text: str | None = None, ui_request=None) -> RpcEvent:
    from telegram_pi_bot.model import RuntimeEventKind

    if ui_request is not None:
        kind = RuntimeEventKind.UI_REQUEST
    elif source_type == "agent_settled":
        kind = RuntimeEventKind.SETTLED
    elif source_type.startswith("tool_execution_"):
        kind = RuntimeEventKind.TOOL_ACTIVITY
    else:
        kind = RuntimeEventKind.PROGRESS
    return RpcEvent(kind, source_type, text=text, ui_request=ui_request)


def text_delta(text: str) -> RpcEvent:
    from telegram_pi_bot.model import RuntimeEventKind

    return RpcEvent(RuntimeEventKind.ASSISTANT_TEXT, "message_update", text=text)


async def discard_event(_event) -> None:
    return None


def turn_request(text: str, session=None, *, attachments=()):
    from telegram_pi_bot.model import (
        ModelRef,
        PendingSession,
        SessionConfig,
        SessionRef,
        TurnContent,
        TurnRequest,
    )

    if session is None:
        session = PendingSession(
            SessionRef("pending", "01234567-89ab-cdef-0123-456789abcdef"),
            None,
            SessionConfig(ModelRef("ollama", "qwen3.8-orcarouter:latest"), "medium"),
            1,
        )
    return TurnRequest(session, TurnContent(text, tuple(attachments)))


def scripted_runtime(
    steps: Sequence[RpcResponse | RpcEvent],
    *,
    sessions_dir: Path | None = None,
    turn_timeout_seconds: float = 6 * 60 * 60,
    ui_timeout_seconds: float = 10 * 60,
    materialize_on_prompt: bool = False,
    image_loader=None,
):
    from dataclasses import replace

    from telegram_pi_bot.pi_runtime import PiRuntime

    settings = replace(
        RuntimeSettings(),
        turn_timeout_seconds=turn_timeout_seconds,
        ui_timeout_seconds=ui_timeout_seconds,
    )
    factory = ScriptedRpcFactory(
        steps, sessions_dir=sessions_dir, materialize_on_prompt=materialize_on_prompt
    )
    return (
        PiRuntime(
            settings,
            rpc_factory=factory,
            sessions_dir=sessions_dir,
            image_loader=image_loader,
        ),
        factory,
    )


def scripted_active_runtime(*, clear_queue: str = "success", **kwargs):
    steps: list[RpcResponse | RpcEvent] = [rpc_response("prompt", {"disposition": "started"})]
    steps.append(rpc_response("steer", {"disposition": "queued"}))
    if clear_queue == "eof":
        steps.append(rpc_response("clear_queue", {}, success=False, error="child exited"))
    else:
        steps.append(rpc_response("clear_queue", {"steeringMessages": [], "followUpMessages": []}))
    steps.append(rpc_response("abort", {}))
    return scripted_runtime(steps, **kwargs)


class ScriptedRpcProcess:
    def __init__(self, factory: "ScriptedRpcFactory", event_sink) -> None:
        self.factory = factory
        self.event_sink = event_sink
        self.commands: list[dict[str, Any]] = []
        self.cancelled_dialogs: list[str] = []
        self.closed = False
        self._terminal = asyncio.get_running_loop().create_future()

    @property
    def child_pid(self) -> int:
        return 10_002

    async def request(self, record: Mapping[str, Any], timeout: float) -> RpcResponse:
        self.commands.append(dict(record))
        command = record["type"]
        if command == "prompt" and self.factory.materialize_on_prompt:
            self.factory._write_materialized_header(self.factory.argv[-1])
        if command == "prompt":
            for item in self.factory.events:
                await self.event_sink(item)
            self.factory.events.clear()
        responses = self.factory.responses.get(command)
        if responses:
            return responses.pop(0)
        data: Any = {"disposition": "started"} if command == "prompt" else {}
        if command == "get_last_assistant_text":
            data = {"text": None}
        return RpcResponse(f"fixture-{len(self.commands)}", command, True, data)

    async def cancel_dialog(self, request_id: str) -> None:
        self.cancelled_dialogs.append(request_id)

    async def answer_dialog(
        self, request_id: str, method: str, response: UiResponse
    ) -> None:
        record: dict[str, Any] = {
            "type": "extension_ui_response",
            "id": request_id,
        }
        if method == "select":
            record["value"] = response.value
        elif method == "confirm":
            record["confirmed"] = response.value
        else:
            raise ValueError("unsupported dialog method")
        self.commands.append(record)

    async def wait_for_terminal(self):
        return await self._terminal

    async def close(self) -> None:
        self.closed = True
        if not self._terminal.done():
            self._terminal.set_result(RpcClosed("fixture closed"))

    async def terminate(self) -> None:
        self.closed = True
        self.factory.own_child_terminated = True
        if not self._terminal.done():
            self._terminal.set_result(RpcClosed("fixture terminated"))


class ScriptedRpcFactory:
    """A typed fake at the RpcProcess boundary; no PiRuntime internals are patched."""

    def __init__(
        self,
        steps: Sequence[RpcResponse | RpcEvent],
        *,
        sessions_dir: Path | None = None,
        materialize_on_prompt: bool = False,
    ) -> None:
        self.responses: dict[str, list[RpcResponse]] = {}
        self.events: list[RpcEvent] = []
        for item in steps:
            if isinstance(item, RpcResponse):
                self.responses.setdefault(item.command, []).append(item)
            else:
                self.events.append(item)
        self.sessions_dir = sessions_dir
        self.materialize_on_prompt = materialize_on_prompt
        self.processes: list[ScriptedRpcProcess] = []
        self.argv: list[tuple[str, ...]] = []
        self.cwd: list[Path] = []
        self.env: list[dict[str, str]] = []
        self.own_child_terminated = False
        self.external_process_touched = False

    async def __call__(self, argv, cwd, env, event_sink) -> ScriptedRpcProcess:
        self.argv.append(tuple(argv))
        self.cwd.append(cwd)
        self.env.append(dict(env))
        process = ScriptedRpcProcess(self, event_sink)
        self.processes.append(process)
        return process

    @property
    def commands(self) -> list[dict[str, Any]]:
        return [command for process in self.processes for command in process.commands]

    @property
    def cancelled_dialogs(self) -> list[str]:
        return [item for process in self.processes for item in process.cancelled_dialogs]

    def fail_process(self) -> None:
        process = self.processes[-1]
        if not process._terminal.done():
            process._terminal.set_result(RpcClosed("fixture unexpected EOF"))

    def _write_materialized_header(self, argv: Sequence[str]) -> None:
        if self.sessions_dir is None:
            return
        session_id = _argument(argv, "--session-id")
        if session_id is None:
            return
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        header = {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": "2026-10-03T00:00:00Z",
            "cwd": "/home/alice",
        }
        (self.sessions_dir / f"{session_id}.jsonl").write_text(
            __import__("json").dumps(header) + "\n", encoding="utf-8"
        )


class FakeClock:
    """Small deterministic clock used by application integration tests."""

    def __init__(self, now_ms: int = 1_000) -> None:
        self._now_ms = now_ms
        self._timers: list[tuple[int, int, Any]] = []
        self._sleepers: list[tuple[int, asyncio.Future[None]]] = []
        self._sequence = 0

    def time_ms(self) -> int:
        return self._now_ms

    def now_ms(self) -> int:
        return self._now_ms

    async def sleep_until(self, due_at_ms: int) -> None:
        if due_at_ms <= self._now_ms:
            return
        future = asyncio.get_running_loop().create_future()
        self._sleepers.append((due_at_ms, future))
        self._sleepers.sort(key=lambda item: item[0])
        try:
            await future
        finally:
            self._sleepers = [item for item in self._sleepers if item[1] is not future]

    def call_at(self, due_at_ms: int, callback) -> int:
        self._sequence += 1
        self._timers.append((due_at_ms, self._sequence, callback))
        self._timers.sort(key=lambda item: (item[0], item[1]))
        return self._sequence

    def cancel(self, timer_id: int) -> None:
        self._timers = [item for item in self._timers if item[1] != timer_id]

    async def advance(self, *, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock cannot move backwards")
        await asyncio.sleep(0)
        target = self._now_ms + int(seconds * 1_000)
        while self._timers and self._timers[0][0] <= target:
            due_at, _timer_id, callback = self._timers.pop(0)
            self._now_ms = due_at
            result = callback()
            if asyncio.iscoroutine(result):
                await result
            await asyncio.sleep(0)
        self._now_ms = target
        for due_at, future in tuple(self._sleepers):
            if due_at <= target and not future.done():
                future.set_result(None)
        for _ in range(8):
            await asyncio.sleep(0)


class FakeTelegram:
    """Authorized Telegram ingress plus the outbound port observed by tests."""

    CHAT_ID = 123456789

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.dispatch = None
        self.reactions: list[tuple[int, str]] = []
        self.final_texts: list[str] = []
        self.progress_texts: list[str] = []
        self.edits: list[tuple[int, str]] = []
        self.documents: list[tuple[Path, str, str]] = []
        self.photos: list[tuple[Path, str, str]] = []
        self.buttons: list[UiRequest] = []
        self.choice_messages: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        self.choice_edits = []
        self._choice_ids = []
        self.fail_text_once = False
        self.downloads: dict[str, bytes] = {}
        self._update_id = 0
        self._message_id = 10_000
        self._blocking_callback: tuple[str, str, int] | None = None

    def bind(self, dispatch) -> None:
        self.dispatch = dispatch

    async def receive(self, *, text: str, message_id: int) -> None:
        from telegram_pi_bot.model import ConversationAction

        await self._send_action(
            ConversationAction(
                "add_text",
                text=text,
                source_message_id=message_id,
                now_ms=self.clock.time_ms(),
            )
        )

    async def receive_photo(self, payload: bytes, *, message_id: int) -> None:
        from telegram_pi_bot.model import ConversationAction

        file_id = f"photo-{message_id}"
        self.downloads[file_id] = payload
        await self._send_action(
            ConversationAction(
                "telegram_photo",
                telegram_file_id=file_id,
                file_size=len(payload),
                filename="photo.png",
                mime_type="image/png",
                source_message_id=message_id,
                now_ms=self.clock.time_ms(),
            )
        )

    async def command(self, command: str) -> None:
        from telegram_pi_bot.telegram_ui import parse_command

        action = parse_command(command)
        if action is not None:
            self._message_id += 1
            await self._send_action(type(action)(action.kind, selector=action.selector, **dict(action.values), source_message_id=self._message_id))

    async def callback(self, data: str) -> None:
        from telegram_pi_bot.telegram_ui import parse_callback

        action, key, generation = parse_callback(data)
        await self._send_named_action("telegram_callback", callback_action=action, callback_key=key, generation=generation)

    async def send_now(self, bundle_id: str) -> None:
        await self._send_named_action("send_now", bundle_id=bundle_id)

    async def steer_current(self, bundle_id: str) -> None:
        await self._send_named_action("steer_current", bundle_id=bundle_id)

    async def cancel_bundle(self, bundle_id: str) -> None:
        await self._send_named_action("cancel_bundle", bundle_id=bundle_id)

    async def press_choice(self, label: str, *, message: int = -1) -> None:
        from telegram_pi_bot.telegram_ui import parse_callback

        _text, choices = self.choice_messages[message]
        matches = [data for item_label, data in choices if item_label == label]
        if len(matches) != 1:
            raise AssertionError(f"expected one {label!r} button")
        parsed = parse_callback(matches[0])
        if parsed is None:
            raise AssertionError("fake received invalid callback data")
        action, key, generation = parsed
        await self._send_named_action(
            "telegram_callback",
            callback_action=action,
            callback_key=key,
            generation=generation,
            callback_query_id=f"fake-callback-{len(self.choice_messages)}-{label}",
        )

    async def answer_ui(self, response: str | bool) -> None:
        if self._blocking_callback is None:
            raise AssertionError("no blocking UI callback is pending")
        action, key, generation = self._blocking_callback
        await self._send_named_action(
            "telegram_callback",
            callback_action=action,
            callback_key=key,
            generation=generation,
            response=response,
        )

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        if chat_id != self.CHAT_ID:
            raise AssertionError("test attempted delivery to an unauthorized chat")
        self.reactions.append((message_id, emoji))

    async def send_text(self, chat_id: int, text: str) -> int:
        if chat_id != self.CHAT_ID:
            raise AssertionError("test attempted delivery to an unauthorized chat")
        if self.fail_text_once:
            self.fail_text_once = False
            raise RuntimeError("scripted Telegram send failure")
        self._message_id += 1
        if text.startswith("Working:"):
            self.progress_texts.append(text)
        else:
            self.final_texts.append(text)
        return self._message_id

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        if chat_id != self.CHAT_ID:
            raise AssertionError("test attempted delivery to an unauthorized chat")
        self.edits.append((message_id, text))

    async def download_file(self, file_id: str, max_bytes: int):
        payload = self.downloads[file_id]
        if len(payload) > max_bytes:
            raise AssertionError("fake download exceeded the requested bound")
        yield payload

    async def send_document(
        self, chat_id: int, path: Path, filename: str, caption: str
    ) -> None:
        if chat_id != self.CHAT_ID:
            raise AssertionError("test attempted delivery to an unauthorized chat")
        self.documents.append((path, filename, caption))

    async def send_photo(
        self, chat_id: int, path: Path, filename: str, caption: str
    ) -> None:
        if chat_id != self.CHAT_ID:
            raise AssertionError("test attempted delivery to an unauthorized chat")
        self.photos.append((path, filename, caption))

    async def send_choices(
        self,
        chat_id: int,
        text: str,
        choices: tuple[tuple[str, str], ...],
    ) -> int:
        if chat_id != self.CHAT_ID:
            raise AssertionError("test attempted delivery to an unauthorized chat")
        self._message_id += 1
        self.choice_messages.append((text, choices))
        self._choice_ids.append(self._message_id)
        return self._message_id

    async def edit_choices(self, chat_id, message_id, text, choices):
        if chat_id != self.CHAT_ID:
            raise AssertionError("unauthorized edit")
        index = self._choice_ids.index(message_id)
        self.choice_messages[index] = (text, choices)
        self.choice_edits.append((message_id, text, choices))

    async def check_identity(self):
        return True

    async def show_ui(self, request: UiRequest, callback: tuple[str, str, int]) -> None:
        self.buttons.append(request)
        self._blocking_callback = callback

    async def _send_action(self, action) -> None:
        if self.dispatch is None:
            raise RuntimeError("fake Telegram is not bound to an application")
        values = dict(action.values)
        values.setdefault("now_ms", self.clock.time_ms())
        self._update_id += 1
        values.setdefault("update_id", self._update_id)
        await self.dispatch(
            type(action)(
                action.kind,
                text=action.text,
                selector=action.selector,
                **values,
            )
        )

    async def _send_named_action(self, kind: str, **values) -> None:
        from telegram_pi_bot.model import ConversationAction

        await self._send_action(
            ConversationAction(kind, now_ms=self.clock.time_ms(), **values)
        )


class FakeRuntime:
    """Controlled PiRuntime port; tests decide when events and settlement arrive."""

    def __init__(self, *, default_model: tuple[str, str] = ("ollama", "qwen3.8-orcarouter:latest")) -> None:
        self.default_model = default_model
        self.prompts: list[str] = []
        self.requests = []
        self.active_turns: list[FakeActiveTurn] = []
        self.config_changes = []
        self.compactions = []
        self._materialized: set[str] = set()
        self._snapshots: dict[str | None, Any] = {}
        self._start_gate: asyncio.Future[None] | None = None
        self.reject_next = False

    async def inspect(self, session, *, config=None):
        from telegram_pi_bot.model import RuntimeSnapshot

        snapshot = self._snapshots.get(session.id if session else None, RuntimeSnapshot())
        if config is not None:
            from dataclasses import replace
            return replace(snapshot, selected_model=config.model, thinking=config.thinking)
        return snapshot

    async def list_sessions(self, limit: int):
        return []

    def new_pending_session(self, name: str | None = None):
        from telegram_pi_bot.model import PendingSession, SessionRef

        return PendingSession(
            SessionRef("pending", str(uuid.uuid4())),
            name,
            SessionConfig(ModelRef(*self.default_model), "medium"),
            1_000,
        )

    async def configure_session(self, session, changes):
        from dataclasses import replace

        self.config_changes.append((session, changes))
        snapshot = await self.inspect(session)
        updated = replace(snapshot, selected_model=changes.model or snapshot.selected_model,
                          thinking=changes.thinking or snapshot.thinking,
                          session_name=changes.name or snapshot.session_name)
        self._snapshots[session.id] = updated
        return updated

    async def compact(self, session, instructions):
        from telegram_pi_bot.model import CompactionResult

        self.compactions.append((session, instructions))
        return CompactionResult(True, 10, 5)

    async def resolve_skill(self, name):
        from telegram_pi_bot.model import SkillRef

        if self._snapshots:
            matches = [skill for skill in (await self.inspect(None)).skills if skill.name == name]
            if len(matches) != 1:
                raise LookupError("skill unavailable")
            return matches[0]
        return SkillRef(name)

    async def start_turn(self, request, events):
        if self._start_gate is not None:
            gate = self._start_gate
            await gate
            self._start_gate = None
        self.requests.append(request)
        self.prompts.append(request.content.text)
        turn = FakeActiveTurn(request, events, accepted=not self.reject_next)
        self.reject_next = False
        self.active_turns.append(turn)
        return turn

    def block_next_start(self) -> None:
        self._start_gate = asyncio.get_running_loop().create_future()

    async def release_start(self) -> None:
        if self._start_gate is None:
            raise AssertionError("Pi start is not blocked")
        self._start_gate.set_result(None)
        for _ in range(12):
            await asyncio.sleep(0)

    def session_file_exists(self, session_id: str) -> bool:
        return session_id in self._materialized

    def first_launch_flags(self):
        request = self.requests[0]
        session = request.session
        return (
            "--session-id",
            session.session_id,
            "--name",
            session.name,
            "--provider",
            session.provider,
            "--model",
            session.model_id,
            "--thinking",
            session.thinking,
        )

    async def emit_prompt_accepted(self, disposition: str = "started") -> None:
        if disposition not in {"started", "queued", "handled"}:
            raise ValueError("unsupported fake prompt disposition")
        await asyncio.sleep(0)

    async def emit_text(self, text: str) -> None:
        await self._current().emit(RuntimeEvent(RuntimeEventKind.ASSISTANT_TEXT, text=text))

    async def emit_progress(self, summary: str) -> None:
        await self._current().emit(RuntimeEvent(RuntimeEventKind.PROGRESS, summary=summary))

    async def emit_ui_request(self, request: UiRequest) -> None:
        await self._current().emit(RuntimeEvent(RuntimeEventKind.UI_REQUEST, ui_request=request))

    async def emit_artifact(self, receipt: ArtifactReceipt) -> None:
        await self._current().emit(RuntimeEvent(RuntimeEventKind.ARTIFACT, artifact=receipt))

    async def emit_settled(self, *, status: TurnStatus = TurnStatus.COMPLETED) -> None:
        from telegram_pi_bot.model import PendingSession

        turn = self._current()
        session = turn.request.session
        native = None
        if isinstance(session, PendingSession) and session.session_id in self._materialized:
            native = NativeSession(
                NativeSessionRef(session.session_id),
                session.name,
                session.created_at_ms,
                session.created_at_ms,
                session.config,
            )
        await turn.finish(TurnResult(status, text=turn.text, artifacts=tuple(turn.artifacts), materialized_session=native))
        for _ in range(8):
            await asyncio.sleep(0)

    async def materialize_session(self, session_id: str) -> None:
        self._materialized.add(session_id)

    async def disconnect(self) -> None:
        await self._current().disconnect()
        for _ in range(8):
            await asyncio.sleep(0)

    def _current(self) -> "FakeActiveTurn":
        if not self.active_turns:
            raise AssertionError("Pi has not started a turn")
        return self.active_turns[-1]

    @property
    def prompt_count(self) -> int:
        return len(self.prompts)


class FakeActiveTurn:
    def __init__(self, request, events, *, accepted: bool = True) -> None:
        self.request = request
        self.events = events
        self.text: str | None = None
        self.artifacts: list[ArtifactReceipt] = []
        self.responses: list[tuple[str, UiResponse]] = []
        self.cancelled_ui: list[str] = []
        self.steered: list[Any] = []
        self.aborted = False
        self._accepted = accepted
        self._result = asyncio.get_running_loop().create_future()
        if not accepted:
            self._result.set_result(
                TurnResult(TurnStatus.REJECTED, "Pi rejected the turn.")
            )

    @property
    def accepted(self) -> bool:
        return self._accepted

    async def emit(self, event: RuntimeEvent) -> None:
        if event.kind is RuntimeEventKind.ASSISTANT_TEXT and event.text is not None:
            self.text = (self.text or "") + event.text
        elif event.kind is RuntimeEventKind.ARTIFACT and event.artifact is not None:
            self.artifacts.append(event.artifact)
        await self.events(event)

    async def finish(self, result: TurnResult) -> None:
        await self.events(RuntimeEvent(RuntimeEventKind.SETTLED))
        if not self._result.done():
            self._result.set_result(result)

    async def disconnect(self) -> None:
        if not self._result.done():
            self._result.set_result(TurnResult(TurnStatus.UNCERTAIN))

    async def steer(self, content) -> None:
        self.steered.append(content)

    async def answer_ui(self, request_id: str, response: UiResponse) -> None:
        self.responses.append((request_id, response))

    async def cancel_ui(self, request_id: str) -> None:
        self.cancelled_ui.append(request_id)

    async def abort(self) -> None:
        self.aborted = True
        await self.finish(TurnResult(TurnStatus.ABORTED))

    async def wait(self) -> TurnResult:
        return await self._result

    async def close(self) -> None:
        return None


@dataclass
class FakeSystem:
    """Test composition: real control store, fake clock/ports, app under test."""

    app: Any
    store: Any
    telegram: FakeTelegram
    clock: FakeClock
    pi: FakeRuntime
    root: Path
    _temporary: Any

    CHAT_ID = FakeTelegram.CHAT_ID

    @classmethod
    async def start(cls, *, root: Path | None = None, models=None, skills=None) -> "FakeSystem":
        from telegram_pi_bot.app import BotApplication
        from telegram_pi_bot.effects import EffectRunner
        from telegram_pi_bot.media import AttachmentPolicy, AttachmentStore, GroqTranscriber
        from telegram_pi_bot.outbound import DeliveryQueue
        from telegram_pi_bot.store import ControlStore
        from telegram_pi_bot.telegram_ui import ProgressRenderer

        temporary = None
        if root is None:
            temporary = tempfile.TemporaryDirectory(prefix="telegram-pi-test-")
            root = Path(temporary.name)
        root.mkdir(parents=True, exist_ok=True)
        config = _fake_config(root)
        store = ControlStore(root / "control.sqlite3")
        clock = FakeClock()
        telegram = FakeTelegram(clock)
        pi = FakeRuntime()
        if models is not None or skills is not None:
            from dataclasses import replace
            from telegram_pi_bot.model import SkillRef

            catalog, _factory = runtime_with_models(models or [model("ollama", "qwen3.8-orcarouter:latest")])
            snapshot = await catalog.inspect(None)
            pi._snapshots[None] = replace(snapshot, skills=tuple(SkillRef(name, description) for name, description in (skills or [])))
        telegram.bind(None)
        attachments = AttachmentStore(AttachmentPolicy.from_config(config))
        delivery = DeliveryQueue(
            root / "delivery.sqlite3",
            FakeTelegram.CHAT_ID,
            staging_root=root,
        )
        effects = EffectRunner(
            chat_id=FakeTelegram.CHAT_ID,
            store=store,
            runtime=pi,
            telegram=telegram,
            delivery=delivery,
            progress=ProgressRenderer(telegram, throttle_ms=0),
            attachment_store=attachments,
            clock=clock,
        )
        app = BotApplication(
            config=config,
            store=store,
            runtime=pi,
            telegram=telegram,
            attachment_store=attachments,
            transcriber=GroqTranscriber(config.groq_key_file),
            delivery=delivery,
            effects=effects,
            clock=clock,
        )
        telegram.bind(app.dispatch)
        await app.start()
        return cls(app, store, telegram, clock, pi, root, temporary)

    async def restart(self) -> "FakeSystem":
        await self.app.stop()
        restarted = await self.start(root=self.root)
        restarted.telegram._update_id = self.telegram._update_id
        return restarted

    async def close(self) -> None:
        await self.app.stop()
        if self._temporary is not None:
            self._temporary.cleanup()


def _fake_config(root: Path):
    from telegram_pi_bot.config import BotConfig

    return BotConfig(
        allowed_user_id=FakeTelegram.CHAT_ID,
        private_chat_only=True,
        text_delay_seconds=5,
        media_delay_seconds=10,
        ui_timeout_seconds=600,
        turn_timeout_seconds=6 * 60 * 60,
        cwd=Path("/home/alice"),
        pi_cli=Path("/usr/bin/pi"),
        state_dir=root,
        groq_key_file=root / "groq-key",
        pi_agent_dir=root / ".pi/agent",
        sessions_dir=root / ".pi/agent/sessions",
        default_provider="ollama",
        default_model_id="qwen3.8-orcarouter:latest",
        default_thinking="medium",
        agy_provider="antigravity",
        agy_model_id="gemini-3.7-flash",
        agy_thinking="high",
        inbound_items=10,
        inbound_bundle_bytes=50 * 1024 * 1024,
        voice_bytes=20 * 1024 * 1024,
        document_bytes=20 * 1024 * 1024,
        photo_bytes=10 * 1024 * 1024,
        outbound_artifacts_per_turn=5,
        outbound_total_bytes=50 * 1024 * 1024,
        outbound_file_bytes=20 * 1024 * 1024,
        outbound_image_bytes=10 * 1024 * 1024,
        staging_retention_seconds=86_400,
        metadata_retention_seconds=30 * 86_400,
        telegram_bot_token="test-token",
        config_path=root / "config.toml",
    )
