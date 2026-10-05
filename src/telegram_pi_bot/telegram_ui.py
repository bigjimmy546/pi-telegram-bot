"""Transport-neutral parsing and bounded Telegram rendering helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Protocol

from telegram import BotCommand

from telegram_pi_bot.model import ConversationAction, RuntimeEvent, RuntimeEventKind


TELEGRAM_TEXT_LIMIT = 4096
_COMMAND_TEXT_LIMIT = 4096
_NAME_LIMIT = 80
_SELECTOR_LIMIT = 128
_CALLBACK_ACTION = re.compile(r"[a-z_][a-z0-9_]{0,11}")
_SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

BOT_COMMANDS = (
    BotCommand("new", "Start a new session"),
    BotCommand("sessions", "List sessions"),
    BotCommand("use", "Select a session"),
    BotCommand("model", "Choose model"),
    BotCommand("thinking", "Choose thinking level"),
    BotCommand("skill", "Browse or invoke a skill"),
    BotCommand("compact", "Compact the selected session"),
    BotCommand("status", "Show current status"),
    BotCommand("stop", "Stop the active turn"),
    BotCommand("usage", "Show session usage"),
    BotCommand("help", "Show help"),
)


def parse_command(text: object) -> ConversationAction | None:
    """Parse the closed command surface without performing state changes."""

    if not isinstance(text, str) or not text.startswith("/") or len(text) > _COMMAND_TEXT_LIMIT:
        return None
    command, separator, remainder = text.partition(" ")
    argument = remainder.strip() if separator else ""
    if command in {"/start", "/help"} and not argument:
        return ConversationAction("command_help")
    if command in {"/clear", "/reset"} and not argument:
        return ConversationAction("command_new")
    if command == "/new":
        if len(argument) > _NAME_LIMIT:
            return None
        return ConversationAction("command_new", name=argument or None)
    if command == "/sessions":
        values = argument.split() if argument else []
        show_all = values[:1] == ["all"]
        if show_all:
            values = values[1:]
        elif values:
            return None
        if len(values) > 1:
            return None
        limit = 10
        if values:
            if not values[0].isdecimal() or not 1 <= int(values[0]) <= 50:
                return None
            limit = int(values[0])
        return ConversationAction("command_sessions", show_all=show_all, limit=limit)
    if command == "/use":
        return (
            ConversationAction("command_use", selector=argument)
            if _bounded(argument, _SELECTOR_LIMIT) and " " not in argument
            else None
        )
    if command == "/model":
        if not argument:
            return ConversationAction("command_model_catalog")
        return (
            ConversationAction("command_model", selector=argument)
            if _bounded(argument, _SELECTOR_LIMIT) and " " not in argument
            else None
        )
    if command == "/thinking":
        if not argument:
            return ConversationAction("command_thinking_catalog")
        return (
            ConversationAction("command_thinking", selector=argument)
            if _bounded(argument, 32) and " " not in argument
            else None
        )
    if command == "/skill":
        if not argument:
            return ConversationAction("command_skill_catalog")
        name, request_separator, request = argument.partition(" ")
        request = request.strip()
        if (
            not request_separator
            or not request
            or not _SKILL_NAME.fullmatch(name)
            or len(request) > _COMMAND_TEXT_LIMIT
        ):
            return None
        return ConversationAction("command_skill", name=name, request=request)
    if command == "/compact":
        return ConversationAction(
            "command_compact", instructions=argument or None
        )
    simple = {
        "/status": "command_status",
        "/stop": "stop",
        "/usage": "command_usage",
        "/doctor": "command_doctor",
    }
    if command in simple and not argument:
        return ConversationAction(simple[command])
    return None


def callback_data(action: str, key: str, generation: int) -> str:
    """Encode a bounded opaque callback with a short integrity tag."""

    if (
        not _CALLBACK_ACTION.fullmatch(action)
        or not isinstance(key, str)
        or not key
        or len(key.encode("utf-8")) > 20
        or type(generation) is not int
        or not 0 <= generation <= 2_147_483_647
    ):
        raise ValueError("invalid callback fields")
    token = base64.urlsafe_b64encode(key.encode("utf-8")).rstrip(b"=").decode("ascii")
    signed = f"{action}:{generation}:{token}".encode("ascii")
    tag = base64.urlsafe_b64encode(hashlib.blake2s(signed, digest_size=4).digest()).rstrip(b"=").decode("ascii")
    result = f"v1:{action}:{generation}:{token}:{tag}"
    if len(result.encode("utf-8")) > 64:
        raise ValueError("callback data is too long")
    return result


def parse_callback(value: object) -> tuple[str, str, int] | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 64:
        return None
    parts = value.split(":")
    if (
        len(parts) != 5
        or parts[0] != "v1"
        or not _CALLBACK_ACTION.fullmatch(parts[1])
        or not parts[2].isdecimal()
    ):
        return None
    action, generation_text, token, supplied_tag = parts[1:]
    generation = int(generation_text)
    if generation > 2_147_483_647:
        return None
    signed = f"{action}:{generation}:{token}".encode("ascii")
    expected_tag = base64.urlsafe_b64encode(
        hashlib.blake2s(signed, digest_size=4).digest()
    ).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(supplied_tag, expected_tag):
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        key = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not key or len(raw) > 20:
        return None
    return action, key, generation


def split_final_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> tuple[str, ...]:
    """Split plain text at readable boundaries without Telegram parse modes."""

    if type(limit) is not int or limit < 1:
        raise ValueError("text limit must be positive")
    if not text:
        return ("Pi completed without a text reply.",)
    parts: list[str] = []
    offset = 0
    while offset < len(text):
        end = min(len(text), offset + limit)
        if end < len(text):
            window = text[offset:end]
            boundary = max(
                window.rfind("\n\n"),
                window.rfind("\n"),
                window.rfind(" "),
            )
            if boundary > 0:
                end = offset + boundary + (
                    2 if text[offset + boundary : offset + boundary + 2] == "\n\n" else 1
                )
        parts.append(text[offset:end])
        offset = end
    return tuple(parts)


class ProgressPort(Protocol):
    async def send_text(self, chat_id: int, text: str) -> int: ...

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None: ...


@dataclass(slots=True)
class _ProgressCard:
    message_id: int
    last_edit_ms: int
    text: str


class ProgressRenderer:
    """Render normalized summaries into at most one throttled card per chat."""

    def __init__(self, port: ProgressPort, *, throttle_ms: int = 1_000) -> None:
        if throttle_ms < 0:
            raise ValueError("progress throttle must be non-negative")
        self._port = port
        self._throttle_ms = throttle_ms
        self._cards: dict[int, _ProgressCard] = {}

    async def publish(
        self, chat_id: int, event: RuntimeEvent, *, now_ms: int
    ) -> int | None:
        summary = _progress_summary(event)
        if summary is None:
            return self._cards.get(chat_id).message_id if chat_id in self._cards else None
        card = self._cards.get(chat_id)
        try:
            if card is None:
                message_id = await self._port.send_text(chat_id, summary)
                self._cards[chat_id] = _ProgressCard(message_id, now_ms, summary)
                return message_id
            if summary == card.text or now_ms - card.last_edit_ms < self._throttle_ms:
                return card.message_id
            await self._port.edit_text(chat_id, card.message_id, summary)
            card.last_edit_ms = now_ms
            card.text = summary
            return card.message_id
        except Exception:
            return None

    def reset(self, chat_id: int) -> None:
        self._cards.pop(chat_id, None)


def _progress_summary(event: RuntimeEvent) -> str | None:
    if event.kind not in {
        RuntimeEventKind.PROGRESS,
        RuntimeEventKind.TOOL_ACTIVITY,
        RuntimeEventKind.WARNING,
    }:
        return None
    fallback = {
        RuntimeEventKind.PROGRESS: "Working…",
        RuntimeEventKind.TOOL_ACTIVITY: "Using tools…",
        RuntimeEventKind.WARNING: "Attention needed",
    }[RuntimeEventKind(event.kind)]
    value = event.summary.strip() if isinstance(event.summary, str) else fallback
    value = " ".join(value.split())
    return (value or fallback)[:512]


def _bounded(value: str, limit: int) -> bool:
    return bool(value) and len(value) <= limit and not any(ord(char) < 32 for char in value)
