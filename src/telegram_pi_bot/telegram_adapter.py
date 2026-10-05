"""Authorization-first Telegram ingress and production Telegram transport."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    ReactionTypeEmoji,
    Update,
)
from telegram.constants import ReactionEmoji
from telegram.ext import Application, ContextTypes, TypeHandler

from telegram_pi_bot.config import BotConfig
from telegram_pi_bot.media import supported_document_metadata
from telegram_pi_bot.model import ConversationAction
from telegram_pi_bot.telegram_ui import BOT_COMMANDS, parse_callback, parse_command


Dispatch = Callable[[ConversationAction], Awaitable[Any]]


class TelegramPort(Protocol):
    async def react(self, chat_id: int, message_id: int, emoji: str) -> None: ...

    async def send_text(self, chat_id: int, text: str) -> int: ...

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None: ...

    async def download_file(self, file_id: str, max_bytes: int) -> AsyncIterator[bytes]: ...

    async def send_document(
        self, chat_id: int, path: Path, filename: str, caption: str
    ) -> None: ...

    async def send_photo(
        self, chat_id: int, path: Path, filename: str, caption: str
    ) -> None: ...

    async def send_choices(
        self,
        chat_id: int,
        text: str,
        choices: tuple[tuple[str, str], ...],
    ) -> int: ...

    async def edit_choices(self, chat_id: int, message_id: int, text: str,
                           choices: tuple[tuple[str, str], ...]) -> None: ...


class TelegramDownloadError(RuntimeError):
    pass


class TelegramAdapter:
    """Keep all python-telegram-bot objects behind one strict boundary."""

    def __init__(
        self,
        config: BotConfig,
        dispatch: Dispatch,
        *,
        port: TelegramPort | None = None,
        http_client: httpx.AsyncClient | None = None,
        now_ms: Callable[[], int] | None = None,
        claim_update: Callable[[int, int], bool] | None = None,
    ) -> None:
        self._config = config
        self._dispatch = dispatch
        self._port: TelegramPort = port or self
        self._http_client = http_client
        self._now_ms = now_ms or (lambda: int(time.time() * 1_000))
        self._claim_update = claim_update
        self._application: Application | None = None

    def build_application(self) -> Application:
        application = Application.builder().token(self._config.telegram_bot_token).build()
        application.add_handler(TypeHandler(Update, self.handle_update))
        self._application = application
        return application

    async def set_commands(self) -> None:
        await self._bot().set_my_commands(BOT_COMMANDS)

    async def check_identity(self) -> bool:
        diagnostic = self._application is None
        if diagnostic:
            self.build_application()
        try:
            await self._bot().initialize()
            identity = await self._bot().get_me()
            return type(identity.id) is int and identity.id > 0 and identity.is_bot is True
        finally:
            if diagnostic:
                await self._bot().shutdown()

    async def handle_update(
        self,
        update: Any,
        context: ContextTypes.DEFAULT_TYPE | None = None,
    ) -> None:
        del context
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        if (
            user is None
            or chat is None
            or getattr(user, "id", None) != self._config.allowed_user_id
            or getattr(chat, "id", None) != getattr(user, "id", None)
            or (
                self._config.private_chat_only
                and getattr(chat, "type", None) != "private"
            )
        ):
            return

        # Edits are stale input, not authorization to run another Pi turn.
        if getattr(update, "edited_message", None) is not None:
            return

        update_id = getattr(update, "update_id", None)
        if type(update_id) is not int or update_id < 0:
            return
        callback = getattr(update, "callback_query", None)
        if callback is not None:
            parsed = parse_callback(getattr(callback, "data", None))
            callback_id = getattr(callback, "id", None)
            if parsed is None or not isinstance(callback_id, str) or not callback_id:
                return
            if not self._claim(update_id):
                return
            callback_action, callback_key, generation = parsed
            try:
                await self._dispatch(
                    ConversationAction(
                        "telegram_callback",
                        callback_action=callback_action,
                        callback_key=callback_key,
                        generation=generation,
                        callback_query_id=callback_id,
                        update_id=update_id,
                        now_ms=self._now_ms(),
                    )
                )
            finally:
                try:
                    await callback.answer()
                except Exception:
                    pass
            return

        message = getattr(update, "effective_message", None)
        message_id = getattr(message, "message_id", None) if message is not None else None
        if type(message_id) is not int or message_id <= 0:
            return
        text = getattr(message, "text", None)
        if isinstance(text, str) and text.startswith("/"):
            command = parse_command(text)
            known_commands = {f"/{item.command}" for item in BOT_COMMANDS} | {
                "/start", "/clear", "/reset", "/doctor",
            }
            if command is None and text.partition(" ")[0] in known_commands:
                return
            if self._claim(update_id):
                if command is None:
                    await self._safe_text(chat.id, "Unknown bot command. Use /help, or send text without a leading /.")
                else:
                    await self._dispatch(_telegram_action(command, update_id, message_id, self._now_ms()))
            return
        if isinstance(text, str) and text:
            await self._accept(
                chat.id,
                message_id,
                ConversationAction(
                    "add_text",
                    text=text,
                    source_message_id=message_id,
                    update_id=update_id,
                    now_ms=self._now_ms(),
                ),
            )
            return

        voice = getattr(message, "voice", None)
        if voice is not None:
            await self._handle_media(
                chat.id,
                message_id,
                update_id,
                kind="voice",
                file_id=getattr(voice, "file_id", None),
                file_size=getattr(voice, "file_size", None),
                filename="voice.ogg",
                mime_type=getattr(voice, "mime_type", None) or "audio/ogg",
                limit=self._config.voice_bytes,
            )
            return
        photos = getattr(message, "photo", ())
        if photos:
            photo = photos[-1]
            await self._handle_media(
                chat.id,
                message_id,
                update_id,
                kind="photo",
                file_id=getattr(photo, "file_id", None),
                file_size=getattr(photo, "file_size", None),
                filename="photo.jpg",
                mime_type="image/jpeg",
                limit=self._config.photo_bytes,
            )
            return
        document = getattr(message, "document", None)
        if document is not None:
            filename = getattr(document, "file_name", None)
            mime_type = getattr(document, "mime_type", None)
            await self._handle_media(
                chat.id,
                message_id,
                update_id,
                kind="document",
                file_id=getattr(document, "file_id", None),
                file_size=getattr(document, "file_size", None),
                filename=filename,
                mime_type=mime_type,
                limit=self._config.document_bytes,
            )

    async def _handle_media(
        self,
        chat_id: int,
        message_id: int,
        update_id: int,
        *,
        kind: str,
        file_id: object,
        file_size: object,
        filename: str | None,
        mime_type: str | None,
        limit: int,
    ) -> None:
        if not isinstance(file_id, str) or not file_id:
            return
        if file_size is not None and (type(file_size) is not int or file_size < 0):
            return
        if file_size is not None and file_size > limit:
            if self._claim(update_id):
                await self._safe_text(chat_id, f"That {kind} exceeds its size limit.")
            return
        if kind == "document" and not supported_document_metadata(filename, mime_type):
            if self._claim(update_id):
                await self._safe_text(chat_id, "That document type is not supported.")
            return
        await self._accept(
            chat_id,
            message_id,
            ConversationAction(
                f"telegram_{kind}",
                telegram_file_id=file_id,
                file_size=file_size,
                filename=filename,
                mime_type=mime_type,
                source_message_id=message_id,
                update_id=update_id,
                now_ms=self._now_ms(),
            ),
        )

    async def _accept(
        self, chat_id: int, message_id: int, action: ConversationAction
    ) -> None:
        if not self._claim(action.get("update_id")):
            return
        try:
            await self._port.react(chat_id, message_id, "👀")
        except Exception:
            pass
        await self._dispatch(action)

    def _claim(self, update_id: int) -> bool:
        return self._claim_update is None or self._claim_update(update_id, self._now_ms())

    async def _safe_text(self, chat_id: int, text: str) -> None:
        try:
            await self._port.send_text(chat_id, text)
        except Exception:
            pass

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        allowed = {
            "👀": ReactionEmoji.EYES,
            "👌": ReactionEmoji.OK_HAND_SIGN,
            "😨": ReactionEmoji.FEARFUL_FACE,
        }
        reaction = allowed.get(emoji)
        if reaction is None:
            raise ValueError("unsupported reaction")
        await self._bot().set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(reaction)],
        )

    async def send_text(self, chat_id: int, text: str) -> int:
        message = await self._bot().send_message(chat_id=chat_id, text=text)
        return int(message.message_id)

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        await self._bot().edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
        )

    async def download_file(self, file_id: str, max_bytes: int) -> AsyncIterator[bytes]:
        if not isinstance(file_id, str) or not file_id or max_bytes <= 0:
            raise TelegramDownloadError("invalid Telegram download request")
        telegram_file = await self._bot().get_file(file_id)
        url = getattr(telegram_file, "file_path", None)
        if not isinstance(url, str):
            raise TelegramDownloadError("Telegram file path is unavailable")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "api.telegram.org":
            raise TelegramDownloadError("Telegram file endpoint is invalid")
        if self._http_client is None:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
                async for chunk in _bounded_download(client, url, max_bytes):
                    yield chunk
            return
        async for chunk in _bounded_download(self._http_client, url, max_bytes):
            yield chunk

    async def send_document(
        self, chat_id: int, path: Path, filename: str, caption: str
    ) -> None:
        with path.open("rb") as handle:
            await self._bot().send_document(
                chat_id=chat_id,
                document=InputFile(handle, filename=filename),
                caption=caption or None,
                read_timeout=60,
                write_timeout=120,
            )

    async def send_photo(
        self, chat_id: int, path: Path, filename: str, caption: str
    ) -> None:
        with path.open("rb") as handle:
            await self._bot().send_photo(
                chat_id=chat_id,
                photo=InputFile(handle, filename=filename),
                caption=caption or None,
                read_timeout=60,
                write_timeout=120,
            )

    async def send_choices(
        self,
        chat_id: int,
        text: str,
        choices: tuple[tuple[str, str], ...],
    ) -> int:
        if not choices:
            raise ValueError("at least one Telegram choice is required")
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data)] for label, data in choices]
        )
        message = await self._bot().send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
        )
        return int(message.message_id)

    def _bot(self):
        if self._application is None:
            raise RuntimeError("Telegram application is not built")
        return self._application.bot

    async def edit_choices(self, chat_id: int, message_id: int, text: str,
                           choices: tuple[tuple[str, str], ...]) -> None:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data)] for label, data in choices]
        )
        await self._bot().edit_message_text(chat_id=chat_id, message_id=message_id,
                                            text=text, reply_markup=keyboard)


def _telegram_action(
    command: ConversationAction,
    update_id: int,
    message_id: int,
    now_ms: int,
) -> ConversationAction:
    return ConversationAction(
        command.kind,
        text=command.text,
        selector=command.selector,
        **dict(command.values),
        update_id=update_id,
        source_message_id=message_id,
        now_ms=now_ms,
    )


async def _bounded_download(
    client: httpx.AsyncClient, url: str, max_bytes: int
) -> AsyncIterator[bytes]:
    try:
        async with client.stream("GET", url, timeout=60.0) as response:
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length is not None and length.isdecimal() and int(length) > max_bytes:
                raise TelegramDownloadError("Telegram file exceeds its size limit")
            total = 0
            async for chunk in response.aiter_bytes(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise TelegramDownloadError("Telegram file exceeds its size limit")
                if chunk:
                    yield chunk
    except TelegramDownloadError:
        raise
    except httpx.HTTPError:
        raise TelegramDownloadError("Telegram file download failed") from None
