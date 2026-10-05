from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram_pi_bot.config import BotConfig
from telegram_pi_bot.telegram_adapter import TelegramAdapter
from telegram_pi_bot.telegram_ui import callback_data


AUTHORIZED_ID = 123456789


def _config() -> BotConfig:
    return BotConfig(
        allowed_user_id=AUTHORIZED_ID,
        private_chat_only=True,
        text_delay_seconds=5,
        media_delay_seconds=10,
        ui_timeout_seconds=600,
        turn_timeout_seconds=21600,
        cwd=Path("/home/alice"),
        pi_cli=Path("/usr/bin/pi"),
        state_dir=Path("/tmp/telegram-pi-test"),
        groq_key_file=Path("/tmp/groq-key"),
        pi_agent_dir=Path("/tmp/telegram-pi-test/.pi/agent"),
        sessions_dir=Path("/tmp/telegram-pi-test/.pi/agent/sessions"),
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
        staging_retention_seconds=86400,
        metadata_retention_seconds=2592000,
        telegram_bot_token="test-token",
        config_path=Path("/tmp/config.toml"),
    )


class RecordingPort:
    def __init__(self, order: list[tuple[object, ...]]) -> None:
        self.order = order
        self.downloads: list[tuple[str, int]] = []

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.order.append(("reaction", message_id, emoji))

    async def send_text(self, chat_id: int, text: str) -> int:
        self.order.append(("text", chat_id, text))
        return 1


class UnauthorizedUpdate:
    update_id = 1
    effective_user = SimpleNamespace(id=7)
    effective_chat = SimpleNamespace(id=7, type="private")

    @property
    def effective_message(self):
        raise AssertionError("unauthorized message content was inspected")

    @property
    def callback_query(self):
        raise AssertionError("unauthorized callback content was inspected")


def _text_update(text: str = "hello", *, chat_type: str = "private"):
    return SimpleNamespace(
        update_id=100,
        effective_user=SimpleNamespace(id=AUTHORIZED_ID),
        effective_chat=SimpleNamespace(id=AUTHORIZED_ID, type=chat_type),
        effective_message=SimpleNamespace(
            message_id=44,
            text=text,
            voice=None,
            photo=(),
            document=None,
        ),
        callback_query=None,
    )


class TelegramAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_update_has_zero_observable_effects(self) -> None:
        actions = []
        order: list[tuple[object, ...]] = []

        async def dispatch(action):
            actions.append(action)

        adapter = TelegramAdapter(_config(), dispatch, port=RecordingPort(order))
        await adapter.handle_update(UnauthorizedUpdate())

        self.assertEqual(actions, [])
        self.assertEqual(order, [])

    async def test_non_private_or_mismatched_chat_is_rejected_before_effects(self) -> None:
        actions = []
        order: list[tuple[object, ...]] = []

        async def dispatch(action):
            actions.append(action)

        adapter = TelegramAdapter(_config(), dispatch, port=RecordingPort(order))
        await adapter.handle_update(_text_update(chat_type="group"))
        mismatch = _text_update()
        mismatch.effective_chat = SimpleNamespace(id=AUTHORIZED_ID + 1, type="private")
        await adapter.handle_update(mismatch)

        self.assertEqual(actions, [])
        self.assertEqual(order, [])

    async def test_authorized_input_reacts_with_eyes_before_dispatch(self) -> None:
        order: list[tuple[object, ...]] = []

        async def dispatch(action):
            order.append(("dispatch", action.kind))
            self.assertEqual(action.text, "hello")
            self.assertEqual(action.get("source_message_id"), 44)
            self.assertEqual(action.get("update_id"), 100)

        adapter = TelegramAdapter(_config(), dispatch, port=RecordingPort(order))
        await adapter.handle_update(_text_update())

        self.assertEqual(
            order,
            [("reaction", 44, "👀"), ("dispatch", "add_text")],
        )

    async def test_reaction_failure_does_not_block_authorized_dispatch(self) -> None:
        actions = []

        class FailingReactionPort(RecordingPort):
            async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
                raise RuntimeError("telegram unavailable")

        async def dispatch(action):
            actions.append(action)

        adapter = TelegramAdapter(
            _config(), dispatch, port=FailingReactionPort([])
        )
        await adapter.handle_update(_text_update())

        self.assertEqual([action.kind for action in actions], ["add_text"])

    async def test_oversized_media_is_rejected_before_reaction_or_dispatch(self) -> None:
        actions = []
        order: list[tuple[object, ...]] = []

        async def dispatch(action):
            actions.append(action)

        update = _text_update()
        update.effective_message.text = None
        update.effective_message.document = SimpleNamespace(
            file_id="large",
            file_size=_config().document_bytes + 1,
            file_name="report.pdf",
            mime_type="application/pdf",
        )
        adapter = TelegramAdapter(_config(), dispatch, port=RecordingPort(order))
        await adapter.handle_update(update)

        self.assertEqual(actions, [])
        self.assertEqual([item[0] for item in order], ["text"])

    async def test_commands_do_not_consume_lifecycle_reactions(self) -> None:
        order: list[tuple[object, ...]] = []

        async def dispatch(action):
            order.append(("dispatch", action.kind))

        adapter = TelegramAdapter(_config(), dispatch, port=RecordingPort(order))
        await adapter.handle_update(_text_update("/skill"))
        self.assertEqual(order, [("dispatch", "command_skill_catalog")])

    async def test_authorized_callback_is_acknowledged_after_dispatch(self) -> None:
        order: list[tuple[object, ...]] = []

        async def dispatch(action):
            order.append(("dispatch", action.kind))

        async def answer():
            order.append(("answer",))

        update = _text_update()
        update.callback_query = SimpleNamespace(
            id="callback-1",
            data=callback_data("send", "opaque", 2),
            answer=answer,
        )
        adapter = TelegramAdapter(_config(), dispatch, port=RecordingPort(order))

        await adapter.handle_update(update)

        self.assertEqual(
            order,
            [("dispatch", "telegram_callback"), ("answer",)],
        )


if __name__ == "__main__":
    unittest.main()
