from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final


class ConfigError(ValueError):
    """A redacted configuration error safe to show in diagnostics."""


PI_SETTINGS_LIMIT: Final = 64 * 1024
THINKING_LEVELS: Final = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_CONFIGURED: Final = object()


_EXPECTED: Final[dict[str, dict[str, object]]] = {
    "bot": {
        "allowed_user_id": _CONFIGURED,
        "private_chat_only": True,
        "text_delay_seconds": 5,
        "media_delay_seconds": 10,
        "ui_timeout_seconds": 600,
        "turn_timeout_seconds": 21600,
    },
    "paths": {
        "cwd": _CONFIGURED,
        "pi_cli": _CONFIGURED,
        "state_dir": _CONFIGURED,
        "groq_key_file": _CONFIGURED,
    },
    "models": {
        "default_provider": "ollama",
        "default_id": "qwen3.8-orcarouter:latest",
        "agy_provider": "antigravity",
        "agy_id": "gemini-3.7-flash",
        "agy_thinking": "high",
    },
    "limits": {
        "inbound_items": 10,
        "inbound_bundle_bytes": 52428800,
        "voice_bytes": 20971520,
        "document_bytes": 20971520,
        "photo_bytes": 10485760,
        "outbound_artifacts": 5,
        "outbound_total_bytes": 52428800,
        "outbound_file_bytes": 20971520,
        "outbound_image_bytes": 10485760,
        "staging_retention_seconds": 86400,
        "metadata_retention_seconds": 2592000,
    },
}


@dataclass(frozen=True, slots=True)
class BotConfig:
    allowed_user_id: int
    private_chat_only: bool
    text_delay_seconds: int
    media_delay_seconds: int
    ui_timeout_seconds: int
    turn_timeout_seconds: int
    cwd: Path
    pi_cli: Path
    state_dir: Path
    groq_key_file: Path
    pi_agent_dir: Path
    sessions_dir: Path
    default_provider: str
    default_model_id: str
    default_thinking: str
    agy_provider: str
    agy_model_id: str
    agy_thinking: str
    inbound_items: int
    inbound_bundle_bytes: int
    voice_bytes: int
    document_bytes: int
    photo_bytes: int
    outbound_artifacts_per_turn: int
    outbound_total_bytes: int
    outbound_file_bytes: int
    outbound_image_bytes: int
    staging_retention_seconds: int
    metadata_retention_seconds: int
    telegram_bot_token: str = field(repr=False)
    config_path: Path = field(repr=False)
    secrets_env_file: Path = field(
        default_factory=lambda: Path.home() / ".config/telegram-pi-bot/secrets.env",
        repr=False,
    )
    groq_base_url: str = "https://api.groq.com/openai/v1/"
    groq_model: str = "whisper-large-v3-turbo"

    @property
    def default_model(self) -> tuple[str, str]:
        return self.default_provider, self.default_model_id

    @property
    def agy_model(self) -> tuple[str, str]:
        return self.agy_provider, self.agy_model_id

    @classmethod
    def load(
        cls,
        path: Path,
        env: Mapping[str, str],
        *,
        require_token: bool = True,
        home: Path | None = None,
        pi_settings_path: Path | None = None,
        project_settings_path: Path | None = None,
    ) -> BotConfig:
        try:
            with path.open("rb") as handle:
                parsed = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError("config file: unreadable or invalid TOML") from error

        _validate_fixed_document(parsed)
        token = env.get("TELEGRAM_BOT_TOKEN")
        if require_token and (not isinstance(token, str) or not token.strip()):
            raise ConfigError("TELEGRAM_BOT_TOKEN: missing or empty")
        token = token if isinstance(token, str) else ""

        home = Path.home() if home is None else home
        bot = parsed["bot"]
        paths = parsed["paths"]
        limits = parsed["limits"]
        parsed_models = parsed["models"]
        allowed_user_id = bot["allowed_user_id"]
        if type(allowed_user_id) is not int or allowed_user_id <= 0:
            raise ConfigError("bot.allowed_user_id: one positive integer is required")
        cwd = _configured_path(paths["cwd"], home=home, label="paths.cwd").resolve()
        pi_cli = _configured_path(paths["pi_cli"], home=home, label="paths.pi_cli")
        state_dir = _configured_path(paths["state_dir"], home=home, label="paths.state_dir")
        groq_key_file = _configured_path(
            paths["groq_key_file"], home=home, label="paths.groq_key_file"
        )
        agent_dir_env = env.get("PI_CODING_AGENT_DIR")
        if agent_dir_env is not None:
            pi_agent_dir = _configured_path(agent_dir_env, home=home, label="PI_CODING_AGENT_DIR")
        else:
            pi_agent_dir = home / ".pi/agent"
        settings = _read_pi_settings(
            pi_settings_path if pi_settings_path is not None else pi_agent_dir / "settings.json",
            required=True,
        )
        merged = dict(settings)
        merged.update(
            _read_pi_settings(
                project_settings_path
                if project_settings_path is not None
                else cwd / ".pi/settings.json",
                required=False,
            )
        )
        default_provider, default_model_id, default_thinking = _pi_defaults(
            merged,
            (parsed_models["agy_provider"], parsed_models["agy_id"]),
            parsed_models["agy_thinking"],
        )
        sessions_dir = _session_directory(
            env=env, settings=merged, home=home, cwd=cwd, agent_dir=pi_agent_dir
        )
        return cls(
            allowed_user_id=allowed_user_id,
            private_chat_only=bot["private_chat_only"],
            text_delay_seconds=bot["text_delay_seconds"],
            media_delay_seconds=bot["media_delay_seconds"],
            ui_timeout_seconds=bot["ui_timeout_seconds"],
            turn_timeout_seconds=bot["turn_timeout_seconds"],
            cwd=cwd,
            pi_cli=pi_cli,
            state_dir=state_dir,
            groq_key_file=groq_key_file,
            pi_agent_dir=pi_agent_dir,
            sessions_dir=sessions_dir,
            default_provider=default_provider,
            default_model_id=default_model_id,
            default_thinking=default_thinking,
            agy_provider=parsed_models["agy_provider"],
            agy_model_id=parsed_models["agy_id"],
            agy_thinking=parsed_models["agy_thinking"],
            inbound_items=limits["inbound_items"],
            inbound_bundle_bytes=limits["inbound_bundle_bytes"],
            voice_bytes=limits["voice_bytes"],
            document_bytes=limits["document_bytes"],
            photo_bytes=limits["photo_bytes"],
            outbound_artifacts_per_turn=limits["outbound_artifacts"],
            outbound_total_bytes=limits["outbound_total_bytes"],
            outbound_file_bytes=limits["outbound_file_bytes"],
            outbound_image_bytes=limits["outbound_image_bytes"],
            staging_retention_seconds=limits["staging_retention_seconds"],
            metadata_retention_seconds=limits["metadata_retention_seconds"],
            telegram_bot_token=token.strip(),
            config_path=path,
            secrets_env_file=path.parent / "secrets.env",
        )

    @staticmethod
    def validate_secret_file(path: Path) -> None:
        _validate_private_file(path, "secret file")

    def validate_startup_paths(self) -> None:
        _validate_private_file(self.config_path, "config file")
        _validate_private_file(self.secrets_env_file, "secrets file")
        self.validate_secret_file(self.groq_key_file)
        if not self.cwd.is_dir():
            raise ConfigError("paths.cwd: directory is unavailable")
        if not self.pi_cli.is_file() or not os.access(self.pi_cli, os.X_OK):
            raise ConfigError("paths.pi_cli: executable is unavailable")


def _validate_fixed_document(parsed: object) -> None:
    if not isinstance(parsed, dict) or set(parsed) != set(_EXPECTED):
        raise ConfigError("config: unexpected or missing section")
    for section_name, expected_fields in _EXPECTED.items():
        section = parsed.get(section_name)
        if not isinstance(section, dict) or set(section) != set(expected_fields):
            raise ConfigError(f"{section_name}: unexpected or missing field")
        for field_name, expected in expected_fields.items():
            actual = section[field_name]
            if expected is _CONFIGURED:
                continue
            if type(actual) is not type(expected) or actual != expected:
                raise ConfigError(f"{section_name}.{field_name}: fixed value required")


def _configured_path(value: object, *, home: Path, label: str) -> Path:
    if not _plain_text(value):
        raise ConfigError(f"{label}: absolute path or ~/ path is required")
    text = str(value)
    if text == "~":
        path = home
    elif text.startswith("~/"):
        path = home / text[2:].lstrip("/")
    else:
        path = Path(text)
    if not path.is_absolute() or path == Path("/"):
        raise ConfigError(f"{label}: absolute scoped path is required")
    try:
        home_resolved = home.resolve()
        resolved = path.resolve()
    except OSError as error:
        raise ConfigError(f"{label}: absolute scoped path is required") from error
    home_scoped = text == "~" or text.startswith("~/")
    if resolved == Path("/") or (
        home_scoped and not resolved.is_relative_to(home_resolved)
    ):
        raise ConfigError(f"{label}: absolute scoped path is required")
    return path


def _session_directory(
    *,
    env: Mapping[str, str],
    settings: Mapping[str, object],
    home: Path,
    cwd: Path,
    agent_dir: Path,
) -> Path:
    configured = env.get("PI_CODING_AGENT_SESSION_DIR") or settings.get("sessionDir")
    if configured is not None:
        if not _plain_text(configured):
            raise ConfigError("Pi settings: invalid session directory")
        text = str(configured)
        home_resolved_check = text == "~" or text.startswith("~/")
        if text == "~":
            path = home
        elif text.startswith("~/"):
            path = home / text[2:].lstrip("/")
        else:
            path = Path(text)
            if not path.is_absolute():
                path = cwd / path
        try:
            home_resolved = home.resolve()
            resolved = path.resolve()
        except OSError as error:
            raise ConfigError("Pi settings: invalid session directory") from error
        if resolved == Path("/") or (
            home_resolved_check and not resolved.is_relative_to(home_resolved)
        ):
            raise ConfigError("Pi settings: invalid session directory")
        return resolved
    encoded = "--" + re.sub(r"[/\\:]", "-", str(cwd).lstrip("/\\")) + "--"
    return (agent_dir / "sessions" / encoded).resolve()


def _pi_defaults(
    settings: Mapping[str, object],
    agy_model: tuple[str, str],
    agy_thinking: str,
) -> tuple[str, str, str]:
    provider = settings.get("defaultProvider")
    model_id = settings.get("defaultModel")
    thinking = settings.get("defaultThinkingLevel", "medium")
    if not _plain_text(provider) or not _plain_text(model_id):
        raise ConfigError("Pi settings: explicit default provider and model are required")
    if not _plain_text(thinking) or thinking not in THINKING_LEVELS:
        raise ConfigError("Pi settings: invalid default thinking level")
    selected = (provider, model_id)
    if provider != "ollama" and selected != agy_model:
        raise ConfigError("Pi settings: default model is outside the bot allowlist")
    if selected == agy_model and thinking != agy_thinking:
        raise ConfigError("Pi settings: Antigravity default requires high thinking")
    return provider, model_id, thinking


def _read_pi_settings(path: Path, *, required: bool) -> Mapping[str, object]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError as error:
        if not required:
            return {}
        raise ConfigError("Pi settings: user settings file is required") from error
    except OSError as error:
        raise ConfigError("Pi settings: settings file is unreadable") from error
    if len(payload) > PI_SETTINGS_LIMIT:
        raise ConfigError("Pi settings: settings file is too large")
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError("Pi settings: invalid JSON") from error
    if not isinstance(parsed, dict):
        raise ConfigError("Pi settings: JSON object is required")
    return parsed


def _plain_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _validate_private_file(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ConfigError(f"{label}: regular file is required") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ConfigError(f"{label}: regular file is required")
    if metadata.st_uid != os.getuid():
        raise ConfigError(f"{label}: current-user ownership is required")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ConfigError(f"{label}: mode 0600 is required")