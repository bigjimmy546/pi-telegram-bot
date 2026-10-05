from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from telegram_pi_bot.model import ArtifactReceipt
from telegram_pi_bot.outbound import DeliveryQueue
from telegram_pi_bot.telegram_adapter import TelegramAdapter
from tests.test_telegram_authorization import _config


AUTHORIZED_ID = 123456789


class FakeOutboundPort:
    def __init__(self, *, fail_text_once: bool = False) -> None:
        self.fail_text_once = fail_text_once
        self.texts: list[tuple[int, str]] = []
        self.documents: list[tuple[int, Path, str, str]] = []
        self.photos: list[tuple[int, Path, str, str]] = []

    async def send_text(self, chat_id: int, text: str) -> int:
        if self.fail_text_once:
            self.fail_text_once = False
            raise RuntimeError("telegram unavailable")
        self.texts.append((chat_id, text))
        return len(self.texts)

    async def send_document(
        self, chat_id: int, path: Path, filename: str, caption: str
    ) -> None:
        self.documents.append((chat_id, path, filename, caption))

    async def send_photo(
        self, chat_id: int, path: Path, filename: str, caption: str
    ) -> None:
        self.photos.append((chat_id, path, filename, caption))


def _receipt(path: Path, *, artifact_id: str = "artifact-1") -> ArtifactReceipt:
    data = path.read_bytes()
    return ArtifactReceipt(
        artifact_id=artifact_id,
        filename="report.txt",
        kind="file",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        staged_path=path,
        caption="Report",
        created_at_ms=1_000,
    )


class DeliveryQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_is_persisted_split_and_each_item_sent_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "staged-copy.txt"
            staged.write_text("artifact")
            queue = DeliveryQueue(root / "delivery.sqlite3", AUTHORIZED_ID)
            delivery_id = queue.enqueue(
                turn_id="turn-1",
                chat_id=AUTHORIZED_ID,
                text="word " * 1_500,
                artifacts=(_receipt(staged),),
                now_ms=1_000,
            )
            port = FakeOutboundPort()

            self.assertTrue(await queue.deliver(delivery_id, port, now_ms=2_000))
            self.assertTrue(all(len(text) <= 4096 for _, text in port.texts))
            self.assertEqual(len(port.documents), 1)
            self.assertFalse(staged.exists())
            self.assertEqual(queue.status(delivery_id).status, "delivered")

            self.assertTrue(await queue.deliver(delivery_id, port, now_ms=3_000))
            self.assertEqual(len(port.documents), 1)

    async def test_retry_survives_reopen_without_rerunning_the_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.sqlite3"
            queue = DeliveryQueue(path, AUTHORIZED_ID)
            delivery_id = queue.enqueue(
                turn_id="turn-2",
                chat_id=AUTHORIZED_ID,
                text="hello",
                now_ms=1_000,
            )
            failing = FakeOutboundPort(fail_text_once=True)

            self.assertFalse(await queue.deliver(delivery_id, failing, now_ms=2_000))
            self.assertEqual(queue.status(delivery_id).status, "pending")
            self.assertEqual(queue.status(delivery_id).attempts, 1)

            reopened = DeliveryQueue(path, AUTHORIZED_ID)
            succeeding = FakeOutboundPort()
            self.assertTrue(
                await reopened.deliver(delivery_id, succeeding, now_ms=3_000)
            )
            self.assertEqual(succeeding.texts, [(AUTHORIZED_ID, "hello")])

    async def test_hash_change_blocks_delivery_and_keeps_staged_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "staged-copy.txt"
            staged.write_text("original")
            queue = DeliveryQueue(root / "delivery.sqlite3", AUTHORIZED_ID)
            delivery_id = queue.enqueue(
                turn_id="turn-3",
                chat_id=AUTHORIZED_ID,
                text="",
                artifacts=(_receipt(staged),),
                now_ms=1_000,
            )
            staged.write_text("changed")
            port = FakeOutboundPort()

            self.assertFalse(await queue.deliver(delivery_id, port, now_ms=2_000))
            self.assertEqual(port.documents, [])
            self.assertTrue(staged.exists())

    async def test_only_authorized_target_and_only_staged_copy_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "project-report.txt"
            original.write_text("artifact")
            staged = root / "staged-copy.txt"
            staged.write_bytes(original.read_bytes())
            queue = DeliveryQueue(root / "delivery.sqlite3", AUTHORIZED_ID)

            with self.assertRaises(ValueError):
                queue.enqueue(
                    turn_id="turn-wrong",
                    chat_id=7,
                    text="no",
                    now_ms=1_000,
                )

            delivery_id = queue.enqueue(
                turn_id="turn-4",
                chat_id=AUTHORIZED_ID,
                text="done",
                artifacts=(_receipt(staged),),
                now_ms=1_000,
            )
            self.assertTrue(
                await queue.deliver(delivery_id, FakeOutboundPort(), now_ms=2_000)
            )
            self.assertTrue(original.exists())
            self.assertFalse(staged.exists())

    async def test_completed_delivery_erases_response_bodies_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "staged-copy.txt"
            staged.write_text("artifact")
            database_path = root / "delivery.sqlite3"
            queue = DeliveryQueue(database_path, AUTHORIZED_ID)
            delivery_id = queue.enqueue(
                turn_id="turn-redact",
                chat_id=AUTHORIZED_ID,
                text="private final answer",
                artifacts=(_receipt(staged),),
                now_ms=1_000,
            )
            self.assertTrue(
                await queue.deliver(delivery_id, FakeOutboundPort(), now_ms=2_000)
            )

            with closing(sqlite3.connect(database_path)) as database:
                retained = tuple(
                    database.execute(
                        "SELECT body, staging_path, caption FROM delivery_items WHERE delivery_id = ?",
                        (delivery_id,),
                    )
                )
            self.assertTrue(retained)
            self.assertTrue(all(row == (None, None, None) for row in retained))

    async def test_interrupted_send_is_uncertain_and_not_automatically_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "delivery.sqlite3"
            queue = DeliveryQueue(database_path, AUTHORIZED_ID)
            delivery_id = queue.enqueue(
                turn_id="turn-inflight",
                chat_id=AUTHORIZED_ID,
                text="maybe delivered",
                now_ms=1_000,
            )
            with closing(sqlite3.connect(database_path)) as database:
                database.execute(
                    "UPDATE delivery_items SET status = 'sending' WHERE delivery_id = ?",
                    (delivery_id,),
                )
                database.commit()

            port = FakeOutboundPort()
            self.assertFalse(await queue.deliver(delivery_id, port, now_ms=2_000))
            self.assertEqual(port.texts, [])
            self.assertEqual(queue.status(delivery_id).status, "uncertain")

            reopened = DeliveryQueue(database_path, AUTHORIZED_ID)
            self.assertFalse(await reopened.deliver(delivery_id, port, now_ms=3_000))
            self.assertEqual(port.texts, [])
            self.assertEqual(reopened.status(delivery_id).status, "uncertain")

    async def test_lost_completion_claim_is_uncertain_and_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            class LostClaimQueue(DeliveryQueue):
                def _mark_item_delivered(self, delivery_id: str, ordinal: int) -> bool:
                    return False

            database_path = Path(directory) / "delivery.sqlite3"
            queue = LostClaimQueue(database_path, AUTHORIZED_ID)
            delivery_id = queue.enqueue(
                turn_id="turn-lost-claim",
                chat_id=AUTHORIZED_ID,
                text="accepted by Telegram",
                now_ms=1_000,
            )
            port = FakeOutboundPort()

            self.assertFalse(await queue.deliver(delivery_id, port, now_ms=2_000))
            self.assertEqual(port.texts, [(AUTHORIZED_ID, "accepted by Telegram")])
            self.assertEqual(queue.status(delivery_id).status, "uncertain")

            reopened = DeliveryQueue(database_path, AUTHORIZED_ID)
            self.assertFalse(await reopened.deliver(delivery_id, port, now_ms=3_000))
            self.assertEqual(port.texts, [(AUTHORIZED_ID, "accepted by Telegram")])

    def test_duplicate_enqueue_reuses_durable_delivery_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = DeliveryQueue(Path(directory) / "delivery.sqlite3", AUTHORIZED_ID)
            first = queue.enqueue(
                turn_id="turn-5",
                chat_id=AUTHORIZED_ID,
                text="first",
                now_ms=1_000,
            )
            second = queue.enqueue(
                turn_id="turn-5",
                chat_id=AUTHORIZED_ID,
                text="different replay",
                now_ms=2_000,
            )
            self.assertEqual(first, second)

    async def test_supported_lifecycle_reactions_are_passed_to_telegram(self) -> None:
        calls = []

        class Bot:
            async def set_message_reaction(self, **kwargs):
                calls.append(kwargs)

        adapter = TelegramAdapter(_config(), lambda action: None)
        adapter._application = SimpleNamespace(bot=Bot())
        for emoji in ("👀", "👌", "😨"):
            await adapter.react(AUTHORIZED_ID, 10, emoji)

        self.assertEqual(
            [call["reaction"][0].emoji for call in calls],
            ["👀", "👌", "😨"],
        )


if __name__ == "__main__":
    unittest.main()
