"""Immutable values shared across the bot's module boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


def _required_id(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"{field_name} must be a non-empty identifier")


@dataclass(frozen=True, slots=True)
class ModelRef:
    provider: str
    model_id: str
    location: str = "local"
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_id(self.provider, "provider")
        _required_id(self.model_id, "model_id")
        _required_id(self.location, "location")
        object.__setattr__(self, "capabilities", tuple(self.capabilities))


@dataclass(frozen=True, slots=True)
class SessionRef:
    kind: str
    id: str

    def __post_init__(self) -> None:
        if self.kind not in {"pending", "native"}:
            raise ValueError("kind must identify a pending or native session")
        _required_id(self.id, "session id")


@dataclass(frozen=True, slots=True)
class NativeSessionRef:
    id: str

    def __post_init__(self) -> None:
        _required_id(self.id, "native session id")


@dataclass(frozen=True, slots=True)
class SessionConfig:
    model: ModelRef
    thinking: str

    def __post_init__(self) -> None:
        _required_id(self.thinking, "thinking")


@dataclass(frozen=True, slots=True)
class SessionConfigChange:
    name: str | None = None
    model: ModelRef | None = None
    thinking: str | None = None

    def __post_init__(self) -> None:
        if self.name is not None and (not self.name or self.name != self.name.strip()):
            raise ValueError("name must be non-empty and trimmed when provided")
        if self.thinking is not None:
            _required_id(self.thinking, "thinking")


@dataclass(frozen=True, slots=True)
class PendingSession:
    ref: SessionRef
    name: str | None
    config: SessionConfig
    created_at_ms: int
    updated_at_ms: int | None = None

    def __post_init__(self) -> None:
        if self.ref.kind != "pending":
            raise ValueError("pending session requires a pending reference")
        if self.name is not None and (
            not isinstance(self.name, str)
            or not self.name
            or self.name != self.name.strip()
        ):
            raise ValueError("pending session name must be non-empty and trimmed")
        updated_at_ms = (
            self.created_at_ms
            if self.updated_at_ms is None
            else self.updated_at_ms
        )
        if (
            type(self.created_at_ms) is not int
            or type(updated_at_ms) is not int
            or self.created_at_ms < 0
            or updated_at_ms < self.created_at_ms
        ):
            raise ValueError("pending session timing is invalid")
        object.__setattr__(self, "updated_at_ms", updated_at_ms)

    @property
    def session_id(self) -> str:
        return self.ref.id

    @property
    def provider(self) -> str:
        return self.config.model.provider

    @property
    def model_id(self) -> str:
        return self.config.model.model_id

    @property
    def thinking(self) -> str:
        return self.config.thinking


@dataclass(frozen=True, slots=True)
class NativeSession:
    ref: NativeSessionRef
    name: str | None
    created_at_ms: int
    updated_at_ms: int
    config: SessionConfig | None


@dataclass(frozen=True, slots=True)
class CompactionResult:
    success: bool
    tokens_before: int
    estimated_tokens_after: int

    def __post_init__(self) -> None:
        if self.tokens_before < 0 or self.estimated_tokens_after < 0:
            raise ValueError("compaction token counts must be non-negative")


@dataclass(frozen=True, slots=True)
class TurnContent:
    text: str
    attachments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "attachments", tuple(self.attachments))


@dataclass(frozen=True, slots=True)
class TurnRequest:
    session: PendingSession | NativeSession
    content: TurnContent


@dataclass(frozen=True, slots=True)
class SkillRef:
    name: str
    description: str = ""

    def __post_init__(self) -> None:
        _required_id(self.name, "skill name")


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    models: tuple[ModelRef, ...] = ()
    thinking_levels: tuple[str, ...] = ()
    skills: tuple[SkillRef, ...] = ()
    session_stats: Mapping[str, int | float | str | None] = field(default_factory=dict)
    provider_readiness: Mapping[str, bool] = field(default_factory=dict)
    selected_model: ModelRef | None = None
    thinking: str | None = None
    session_id: str | None = None
    session_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", tuple(self.models))
        object.__setattr__(self, "thinking_levels", tuple(self.thinking_levels))
        object.__setattr__(self, "skills", tuple(self.skills))
        object.__setattr__(
            self, "session_stats", MappingProxyType(dict(self.session_stats))
        )
        object.__setattr__(
            self, "provider_readiness", MappingProxyType(dict(self.provider_readiness))
        )


class RuntimeEventKind(StrEnum):
    PROGRESS = "progress"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_ACTIVITY = "tool_activity"
    UI_REQUEST = "ui_request"
    ARTIFACT = "artifact"
    WARNING = "warning"
    SETTLED = "settled"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    kind: RuntimeEventKind | str
    text: str | None = None
    summary: str | None = None
    ui_request: UiRequest | None = None
    artifact: ArtifactReceipt | None = None


class TurnStatus(StrEnum):
    REJECTED = "rejected"
    COMPLETED = "completed"
    HANDLED = "handled"
    ABORTED = "aborted"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class TurnResult:
    status: TurnStatus
    text: str | None = None
    artifacts: tuple[ArtifactReceipt, ...] = ()
    materialized_session: NativeSession | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))

    @property
    def retryable(self) -> bool:
        return self.status is TurnStatus.REJECTED


@dataclass(frozen=True, slots=True)
class UiRequest:
    request_id: str
    kind: str
    title: str
    options: tuple[str, ...] = ()
    timeout_ms: int | None = None

    def __post_init__(self) -> None:
        _required_id(self.request_id, "request_id")
        object.__setattr__(self, "options", tuple(self.options))
        if self.timeout_ms is not None and (
            type(self.timeout_ms) is not int or self.timeout_ms <= 0
        ):
            raise ValueError("timeout_ms must be positive when provided")


@dataclass(frozen=True, slots=True)
class UiResponse:
    value: str | bool


@dataclass(frozen=True, slots=True)
class ArtifactRequest:
    path: str
    kind: str
    caption: str = ""

    def __post_init__(self) -> None:
        if not self.path or self.kind not in {"file", "image"}:
            raise ValueError("artifact request path and kind are invalid")


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    artifact_id: str
    filename: str
    kind: str
    size_bytes: int
    sha256: str = ""
    staged_path: Path | None = field(default=None, repr=False)
    caption: str = ""
    created_at_ms: int = 0

    def __post_init__(self) -> None:
        _required_id(self.artifact_id, "artifact_id")
        if self.kind not in {"file", "image"} or self.size_bytes < 0:
            raise ValueError("artifact receipt kind or size is invalid")
        if self.sha256 and (
            len(self.sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.sha256)
        ):
            raise ValueError("artifact receipt hash is invalid")


@dataclass(frozen=True, slots=True)
class BundleItem:
    kind: str
    value: str = field(repr=False)
    source_message_id: int

    def __post_init__(self) -> None:
        if self.kind not in {"text", "voice", "photo", "document"}:
            raise ValueError("bundle item kind is invalid")
        if (
            not isinstance(self.value, str)
            or not self.value
            or type(self.source_message_id) is not int
            or self.source_message_id <= 0
        ):
            raise ValueError("bundle item value and source message are required")


@dataclass(frozen=True, slots=True)
class InputBundle:
    bundle_id: str
    kind: str
    status: str
    items: tuple[BundleItem, ...]
    due_at_ms: int | None
    timer_generation: int
    created_at_ms: int
    expires_at_ms: int
    held: bool = False

    def __post_init__(self) -> None:
        _required_id(self.bundle_id, "bundle id")
        if self.kind not in {"text", "media"} or self.status not in {
            "open",
            "dispatching",
            "queued",
            "frozen",
        }:
            raise ValueError("bundle kind or status is invalid")
        if self.timer_generation < 1 or self.expires_at_ms < self.created_at_ms:
            raise ValueError("bundle timing is invalid")
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True, slots=True)
class TurnRecord:
    turn_id: str
    session_id: str
    source_message_id: int
    status: str
    prompt_accepted: bool
    started_at_ms: int
    finished_at_ms: int | None = None

    def __post_init__(self) -> None:
        _required_id(self.turn_id, "turn id")
        _required_id(self.session_id, "turn session id")
        if self.status not in {
            "dispatching",
            "active",
            "stopping",
            "configuring",
            "compacting",
            "completed",
            "handled",
            "aborted",
            "failed",
            "uncertain",
            "rejected",
        }:
            raise ValueError("turn status is invalid")
        if (
            type(self.source_message_id) is not int
            or self.source_message_id < 0
            or type(self.prompt_accepted) is not bool
            or type(self.started_at_ms) is not int
            or (
                self.finished_at_ms is not None
                and type(self.finished_at_ms) is not int
            )
            or self.started_at_ms < 0
            or (
                self.finished_at_ms is not None
                and self.finished_at_ms < self.started_at_ms
            )
        ):
            raise ValueError("turn metadata is invalid")


@dataclass(frozen=True, slots=True)
class BlockingUiState:
    request_id: str
    turn_id: str
    expires_at_ms: int
    callback_key: str = ""
    generation: int = 0

    def __post_init__(self) -> None:
        _required_id(self.request_id, "blocking UI request id")
        _required_id(self.turn_id, "blocking UI turn id")
        if not self.callback_key:
            object.__setattr__(self, "callback_key", self.request_id)
        if (
            not isinstance(self.callback_key, str)
            or not self.callback_key
            or len(self.callback_key.encode("utf-8")) > 64
            or type(self.generation) is not int
            or self.generation < 0
            or type(self.expires_at_ms) is not int
            or self.expires_at_ms < 0
        ):
            raise ValueError("blocking UI callback is invalid")


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    artifact_id: str
    staging_path: str
    sha256: str
    size: int
    status: str
    expires_at_ms: int

    def __post_init__(self) -> None:
        _required_id(self.artifact_id, "stored artifact id")
        if not self.staging_path or self.size < 0:
            raise ValueError("stored artifact path and size are invalid")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("stored artifact hash is invalid")


@dataclass(frozen=True, slots=True, init=False)
class ConversationAction:
    kind: str
    text: str | None = field(default=None, repr=False)
    selector: str | None
    values: Mapping[str, Any] = field(repr=False)

    def __init__(
        self,
        kind: str,
        text: str | None = None,
        selector: str | None = None,
        **values: Any,
    ) -> None:
        _required_id(kind, "action kind")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "selector", selector)
        object.__setattr__(self, "values", MappingProxyType(dict(values)))

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as error:
            raise AttributeError(name) from error


@dataclass(frozen=True, slots=True)
class Effect:
    kind: str
    value: str | None = None
    effect_id: str = ""
    payload: Mapping[str, int | float | str | bool | None] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        _required_id(self.kind, "effect kind")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class BotState:
    version: int
    chat_id: int
    now_ms: int = 0
    selected_session_id: str | None = None
    selected_session_path: str | None = None
    pending_sessions: Mapping[str, PendingSession] = field(default_factory=dict)
    bundle: InputBundle | None = field(default=None, repr=False)
    next_bundle: InputBundle | None = field(default=None, repr=False)
    steering_bundle: InputBundle | None = field(default=None, repr=False)
    active_turn: TurnRecord | None = None
    active_turn_id: str | None = field(default=None, repr=False)
    blocking_ui: BlockingUiState | None = None
    progress_message_id: int | None = None
    callback_generation: int = 0
    stop_requested: bool = False
    bundles: Mapping[str, InputBundle] = field(default_factory=dict, repr=False)
    turns: Mapping[str, TurnRecord] = field(default_factory=dict, repr=False)
    artifacts: Mapping[str, StoredArtifact] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.version < 0 or self.chat_id <= 0 or self.now_ms < 0:
            raise ValueError("bot state version, chat, and time are invalid")
        pending = dict(self.pending_sessions)
        bundles = dict(self.bundles)
        turns = dict(self.turns)
        artifacts = dict(self.artifacts)
        active = self.active_turn
        if active is None and self.active_turn_id is not None:
            raise ValueError("active turn ID requires an active turn")
        if (
            active is not None
            and self.active_turn_id is not None
            and self.active_turn_id != active.turn_id
        ):
            raise ValueError("active turn ID does not match the active turn")
        if active is not None:
            object.__setattr__(self, "active_turn_id", active.turn_id)
            turns[active.turn_id] = active
        slot_ids = [
            bundle.bundle_id
            for bundle in (self.bundle, self.next_bundle, self.steering_bundle)
            if bundle is not None
        ]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("bundle slots must have distinct identities")
        if self.bundle is not None:
            bundles[self.bundle.bundle_id] = self.bundle
        if self.next_bundle is not None:
            bundles[self.next_bundle.bundle_id] = self.next_bundle
        if self.steering_bundle is not None:
            bundles[self.steering_bundle.bundle_id] = self.steering_bundle
        object.__setattr__(self, "pending_sessions", MappingProxyType(pending))
        object.__setattr__(self, "bundles", MappingProxyType(bundles))
        object.__setattr__(self, "turns", MappingProxyType(turns))
        object.__setattr__(self, "artifacts", MappingProxyType(artifacts))


@dataclass(frozen=True, slots=True)
class Transition:
    state: BotState
    effects: tuple[Effect, ...] = ()
    action_id: str | None = None
    replies: tuple[str, ...] = ()
    settled_effects: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "effects", tuple(self.effects))
        object.__setattr__(self, "replies", tuple(self.replies))
        statuses = dict(self.settled_effects)
        if any(status not in {"done", "failed"} for status in statuses.values()):
            raise ValueError("settled effect status is invalid")
        object.__setattr__(self, "settled_effects", MappingProxyType(statuses))
