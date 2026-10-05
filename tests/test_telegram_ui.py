from __future__ import annotations

import unittest

from telegram_pi_bot.model import RuntimeEvent, RuntimeEventKind
from telegram_pi_bot.telegram_ui import (
    BOT_COMMANDS,
    ProgressRenderer,
    callback_data,
    parse_callback,
    parse_command,
    split_final_text,
)


class FakeProgressPort:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.fail = False

    async def send_text(self, chat_id: int, text: str) -> int:
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.sent.append((chat_id, text))
        return 81

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.edited.append((chat_id, message_id, text))


class TelegramUiTests(unittest.IsolatedAsyncioTestCase):
    def test_visible_botfather_menu_is_exact(self) -> None:
        self.assertEqual(
            tuple(command.command for command in BOT_COMMANDS),
            (
                "new",
                "sessions",
                "use",
                "model",
                "thinking",
                "skill",
                "compact",
                "status",
                "stop",
                "usage",
                "help",
            ),
        )

    def test_skill_direct_and_catalog_paths_are_distinct(self) -> None:
        catalog = parse_command("/skill")
        direct = parse_command("/skill advisor review NVDA")

        self.assertEqual(catalog.kind, "command_skill_catalog")
        self.assertEqual(direct.kind, "command_skill")
        self.assertEqual(direct.get("name"), "advisor")
        self.assertEqual(direct.get("request"), "review NVDA")
        self.assertIsNone(parse_command("/skill advisor"))

    def test_hidden_aliases_and_bounded_command_arguments(self) -> None:
        self.assertEqual(parse_command("/start").kind, "command_help")
        self.assertEqual(parse_command("/clear").kind, "command_new")
        self.assertEqual(parse_command("/reset").kind, "command_new")
        self.assertEqual(parse_command("/doctor").kind, "command_doctor")
        self.assertIsNone(parse_command("/use one two"))
        self.assertIsNone(parse_command("/sessions all 51"))

    def test_callback_payload_is_bounded_and_round_trips_opaque_key(self) -> None:
        encoded = callback_data("send", "opaque-key", 7)
        self.assertLessEqual(len(encoded.encode("utf-8")), 64)
        self.assertNotIn("opaque-key", encoded)
        self.assertEqual(parse_callback(encoded), ("send", "opaque-key", 7))
        self.assertIsNone(parse_callback(encoded + "x"))

    def test_plain_text_split_is_ordered_bounded_and_needs_no_parse_mode(self) -> None:
        text = "<unsafe>\n\n" + "word " * 1_500
        parts = split_final_text(text)
        self.assertTrue(all(0 < len(part) <= 4096 for part in parts))
        self.assertEqual("".join(parts), text)

    async def test_progress_uses_one_throttled_card_and_never_event_text(self) -> None:
        port = FakeProgressPort()
        renderer = ProgressRenderer(port, throttle_ms=1_000)
        first = RuntimeEvent(
            RuntimeEventKind.PROGRESS,
            text="private chain of thought",
            summary="Reading project files",
        )
        second = RuntimeEvent(
            RuntimeEventKind.TOOL_ACTIVITY,
            text="secret tool arguments",
            summary="Running tests",
        )

        await renderer.publish(123456789, first, now_ms=1_000)
        await renderer.publish(123456789, second, now_ms=1_500)
        await renderer.publish(123456789, second, now_ms=2_100)

        self.assertEqual(port.sent, [(123456789, "Reading project files")])
        self.assertEqual(port.edited, [(123456789, 81, "Running tests")])
        self.assertNotIn("private chain", repr(port.sent + port.edited))
        self.assertNotIn("secret tool", repr(port.sent + port.edited))

    async def test_progress_failures_are_non_fatal(self) -> None:
        port = FakeProgressPort()
        port.fail = True
        renderer = ProgressRenderer(port)
        result = await renderer.publish(
            123456789,
            RuntimeEvent(RuntimeEventKind.PROGRESS, summary="Working"),
            now_ms=1_000,
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
