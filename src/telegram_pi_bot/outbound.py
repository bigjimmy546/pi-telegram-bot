"""Durable, idempotent final-text and artifact delivery."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from telegram_pi_bot.artifact_ipc import verify_staged
from telegram_pi_bot.model import ArtifactReceipt
from telegram_pi_bot.telegram_ui import split_final_text


PENDING_RETENTION_MS = 24 * 60 * 60 * 1_000
METADATA_RETENTION_MS = 30 * 24 * 60 * 60 * 1_000


class OutboundPort(Protocol):
    async def send_text(self, chat_id: int, text: str) -> int: ...

    async def send_document(
        self, chat_id: int, path: Path, filename: str, caption: str
    ) -> None: ...

    async def send_photo(
        self, chat_id: int, path: Path, filename: str, caption: str
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DeliveryStatus:
    delivery_id: str
    turn_id: str
    status: str
    attempts: int
    pending_items: int


class DeliveryQueue:
    def __init__(
        self,
        path: Path,
        allowed_chat_id: int,
        *,
        staging_root: Path | None = None,
        max_artifacts: int = 5,
        max_artifact_bytes: int = 50 * 1024 * 1024,
        pending_retention_ms: int = PENDING_RETENTION_MS,
        metadata_retention_ms: int = METADATA_RETENTION_MS,
    ) -> None:
        if not path.is_absolute() or allowed_chat_id <= 0:
            raise ValueError("delivery path and authorized chat are required")
        self.path = path
        self.allowed_chat_id = allowed_chat_id
        self.staging_root = (staging_root or path.parent).absolute()
        self.max_artifacts = max_artifacts
        self.max_artifact_bytes = max_artifact_bytes
        self.pending_retention_ms = pending_retention_ms
        self.metadata_retention_ms = metadata_retention_ms
        self._locks: dict[str, asyncio.Lock] = {}
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as database:
            database.executescript(_SCHEMA)
            database.execute(
                "UPDATE delivery_items SET status = 'uncertain' WHERE status = 'sending'"
            )
            database.execute(
                """
                UPDATE deliveries
                SET status = 'uncertain', last_error = 'delivery_uncertain'
                WHERE EXISTS (
                  SELECT 1 FROM delivery_items
                  WHERE delivery_items.delivery_id = deliveries.delivery_id
                    AND delivery_items.status = 'uncertain'
                )
                """
            )
            database.commit()
        os.chmod(path, 0o600)

    def enqueue(
        self,
        *,
        turn_id: str,
        chat_id: int,
        text: str | None,
        now_ms: int,
        artifacts: Sequence[ArtifactReceipt] = (),
    ) -> str:
        if chat_id != self.allowed_chat_id:
            raise ValueError("delivery target is not authorized")
        if not isinstance(turn_id, str) or not turn_id or now_ms < 0:
            raise ValueError("delivery identity and time are required")
        if len(artifacts) > self.max_artifacts:
            raise ValueError("outbound artifact count exceeded")
        if sum(item.size_bytes for item in artifacts) > self.max_artifact_bytes:
            raise ValueError("outbound artifact total exceeded")
        for receipt in artifacts:
            self._validate_receipt_path(receipt)
        delivery_id = "delivery-" + hashlib.sha256(
            f"{chat_id}:{turn_id}".encode("utf-8")
        ).hexdigest()[:32]
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            existing = database.execute(
                "SELECT delivery_id FROM deliveries WHERE chat_id = ? AND turn_id = ?",
                (chat_id, turn_id),
            ).fetchone()
            if existing is not None:
                database.commit()
                return str(existing[0])
            database.execute(
                """
                INSERT INTO deliveries(
                  delivery_id, turn_id, chat_id, status, attempts,
                  created_at_ms, expires_at_ms, last_error
                ) VALUES (?, ?, ?, 'pending', 0, ?, ?, NULL)
                """,
                (
                    delivery_id,
                    turn_id,
                    chat_id,
                    now_ms,
                    now_ms + self.pending_retention_ms,
                ),
            )
            ordinal = 0
            for part in split_final_text(text or ""):
                database.execute(
                    """
                    INSERT INTO delivery_items(
                      delivery_id, ordinal, kind, body, staging_path,
                      filename, caption, size, sha256, status
                    ) VALUES (?, ?, 'text', ?, NULL, NULL, NULL, NULL, NULL, 'pending')
                    """,
                    (delivery_id, ordinal, part),
                )
                ordinal += 1
            for receipt in artifacts:
                database.execute(
                    """
                    INSERT INTO delivery_items(
                      delivery_id, ordinal, kind, body, staging_path,
                      filename, caption, size, sha256, status
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        delivery_id,
                        ordinal,
                        "photo" if receipt.kind == "image" else "document",
                        str(receipt.staged_path),
                        _safe_filename(receipt.filename),
                        receipt.caption[:1024],
                        receipt.size_bytes,
                        receipt.sha256,
                    ),
                )
                ordinal += 1
            database.commit()
        return delivery_id

    async def deliver(
        self, delivery_id: str, port: OutboundPort, *, now_ms: int
    ) -> bool:
        lock = self._locks.setdefault(delivery_id, asyncio.Lock())
        async with lock:
            record = self._delivery(delivery_id)
            if record is None:
                raise ValueError("unknown delivery")
            if int(record["chat_id"]) != self.allowed_chat_id:
                raise ValueError("delivery target is not authorized")
            if record["status"] == "delivered":
                return True
            if record["status"] == "uncertain":
                return False
            items = self._pending_items(delivery_id)
            for item in items:
                ordinal = int(item["ordinal"])
                if not self._claim_item(delivery_id, ordinal):
                    return False
                try:
                    if item["kind"] == "text":
                        await port.send_text(self.allowed_chat_id, str(item["body"]))
                    else:
                        receipt = ArtifactReceipt(
                            artifact_id=f"{delivery_id}-{item['ordinal']}",
                            filename=str(item["filename"]),
                            kind="image" if item["kind"] == "photo" else "file",
                            size_bytes=int(item["size"]),
                            sha256=str(item["sha256"]),
                            staged_path=Path(str(item["staging_path"])),
                            caption=str(item["caption"] or ""),
                        )
                        self._validate_receipt_path(receipt)
                        verify_staged(receipt)
                        if item["kind"] == "photo":
                            await port.send_photo(
                                self.allowed_chat_id,
                                receipt.staged_path,
                                receipt.filename,
                                receipt.caption,
                            )
                        else:
                            await port.send_document(
                                self.allowed_chat_id,
                                receipt.staged_path,
                                receipt.filename,
                                receipt.caption,
                            )
                except Exception:
                    self._mark_failed(delivery_id, ordinal)
                    return False
                if not self._mark_item_delivered(delivery_id, ordinal):
                    self._mark_uncertain(delivery_id, ordinal)
                    return False
                if item["kind"] != "text":
                    try:
                        self._delete_owned_copy(Path(str(item["staging_path"])))
                    except (OSError, ValueError):
                        pass
            return self._mark_complete(delivery_id, now_ms)

    def status(self, delivery_id: str) -> DeliveryStatus:
        record = self._delivery(delivery_id)
        if record is None:
            raise ValueError("unknown delivery")
        with self._connect() as database:
            pending = int(
                database.execute(
                    "SELECT COUNT(*) FROM delivery_items WHERE delivery_id = ? AND status = 'pending'",
                    (delivery_id,),
                ).fetchone()[0]
            )
        return DeliveryStatus(
            delivery_id=delivery_id,
            turn_id=str(record["turn_id"]),
            status="expired" if record["last_error"] == "delivery_expired" else str(record["status"]),
            attempts=int(record["attempts"]),
            pending_items=pending,
        )

    def latest_status(self) -> str | None:
        with self._connect() as database:
            row = database.execute("SELECT status, last_error FROM deliveries ORDER BY created_at_ms DESC, delivery_id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return "expired" if row["last_error"] == "delivery_expired" else str(row["status"])

    def pending_delivery_ids(self) -> tuple[str, ...]:
        with self._connect() as database:
            return tuple(
                str(row[0])
                for row in database.execute(
                    """
                    SELECT delivery_id FROM deliveries
                    WHERE chat_id = ? AND status = 'pending'
                    ORDER BY created_at_ms, delivery_id
                    """,
                    (self.allowed_chat_id,),
                )
            )

    def expired_delivery_ids(self) -> tuple[str, ...]:
        with self._connect() as database:
            return tuple(str(row[0]) for row in database.execute(
                "SELECT delivery_id FROM deliveries WHERE chat_id = ? AND last_error = 'delivery_expired' ORDER BY created_at_ms, delivery_id",
                (self.allowed_chat_id,),
            ))

    def sweep(self, now_ms: int) -> int:
        removed = 0
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            expired = tuple(
                database.execute(
                    "SELECT delivery_id, status, last_error, created_at_ms FROM deliveries WHERE expires_at_ms <= ?",
                    (now_ms,),
                )
            )
            for row in expired:
                delivery_id = str(row[0])
                for item in database.execute(
                    "SELECT staging_path FROM delivery_items WHERE delivery_id = ? AND staging_path IS NOT NULL",
                    (delivery_id,),
                ):
                    try:
                        self._delete_owned_copy(Path(str(item[0])))
                    except (OSError, ValueError):
                        pass
                if row["status"] != "delivered" and row["last_error"] != "delivery_expired" and int(row["created_at_ms"]) + self.metadata_retention_ms > now_ms:
                    database.execute("DELETE FROM delivery_items WHERE delivery_id = ?", (delivery_id,))
                    database.execute("UPDATE deliveries SET status = 'uncertain', last_error = 'delivery_expired', expires_at_ms = ? WHERE delivery_id = ?", (int(row["created_at_ms"]) + self.metadata_retention_ms, delivery_id))
                else:
                    database.execute("DELETE FROM deliveries WHERE delivery_id = ?", (delivery_id,))
                removed += 1
            database.commit()
        return removed

    def _delivery(self, delivery_id: str) -> sqlite3.Row | None:
        with self._connect() as database:
            return database.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()

    def _pending_items(self, delivery_id: str) -> tuple[sqlite3.Row, ...]:
        with self._connect() as database:
            return tuple(
                database.execute(
                    """
                    SELECT * FROM delivery_items
                    WHERE delivery_id = ? AND status = 'pending'
                    ORDER BY ordinal
                    """,
                    (delivery_id,),
                )
            )

    def _claim_item(self, delivery_id: str, ordinal: int) -> bool:
        with self._connect() as database:
            cursor = database.execute(
                """
                UPDATE delivery_items SET status = 'sending'
                WHERE delivery_id = ? AND ordinal = ? AND status = 'pending'
                """,
                (delivery_id, ordinal),
            )
            database.commit()
            return cursor.rowcount == 1

    def _mark_item_delivered(self, delivery_id: str, ordinal: int) -> bool:
        with self._connect() as database:
            cursor = database.execute(
                """
                UPDATE delivery_items SET
                  status = 'delivered', body = NULL, staging_path = NULL, caption = NULL
                WHERE delivery_id = ? AND ordinal = ? AND status = 'sending'
                """,
                (delivery_id, ordinal),
            )
            database.commit()
            return cursor.rowcount == 1

    def _mark_uncertain(self, delivery_id: str, ordinal: int) -> None:
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                UPDATE delivery_items SET status = 'uncertain'
                WHERE delivery_id = ? AND ordinal = ? AND status != 'delivered'
                """,
                (delivery_id, ordinal),
            )
            database.execute(
                """
                UPDATE deliveries
                SET status = 'uncertain', last_error = 'delivery_uncertain'
                WHERE delivery_id = ? AND status != 'delivered'
                """,
                (delivery_id,),
            )
            database.commit()

    def _mark_failed(self, delivery_id: str, ordinal: int) -> None:
        with self._connect() as database:
            database.execute(
                """
                UPDATE delivery_items SET status = 'pending'
                WHERE delivery_id = ? AND ordinal = ? AND status = 'sending'
                """,
                (delivery_id, ordinal),
            )
            database.execute(
                """
                UPDATE deliveries
                SET attempts = attempts + 1, last_error = 'delivery_failed'
                WHERE delivery_id = ? AND status = 'pending'
                """,
                (delivery_id,),
            )
            database.commit()

    def _mark_complete(self, delivery_id: str, now_ms: int) -> bool:
        with self._connect() as database:
            statuses = {
                str(row[0])
                for row in database.execute(
                    "SELECT status FROM delivery_items WHERE delivery_id = ?",
                    (delivery_id,),
                )
            }
            if statuses & {"sending", "uncertain"}:
                database.execute(
                    """
                    UPDATE delivery_items SET status = 'uncertain'
                    WHERE delivery_id = ? AND status = 'sending'
                    """,
                    (delivery_id,),
                )
                database.execute(
                    """
                    UPDATE deliveries
                    SET status = 'uncertain', last_error = 'delivery_uncertain'
                    WHERE delivery_id = ? AND status != 'delivered'
                    """,
                    (delivery_id,),
                )
                database.commit()
                return False
            if "pending" in statuses:
                database.commit()
                return False
            cursor = database.execute(
                """
                UPDATE deliveries
                SET status = 'delivered', expires_at_ms = ?, last_error = NULL
                WHERE delivery_id = ? AND status = 'pending'
                """,
                (now_ms + self.metadata_retention_ms, delivery_id),
            )
            database.commit()
            return cursor.rowcount == 1

    def _validate_receipt_path(self, receipt: ArtifactReceipt) -> None:
        path = receipt.staged_path
        if path is None or path.absolute().parent != self.staging_root:
            raise ValueError("artifact is outside delivery staging")

    def _delete_owned_copy(self, path: Path) -> None:
        if path.absolute().parent != self.staging_root:
            raise ValueError("artifact is outside delivery staging")
        try:
            details = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("staged artifact is not a regular file")
        path.unlink()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        database = sqlite3.connect(self.path, timeout=5.0)
        try:
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA foreign_keys = ON")
            database.execute("PRAGMA secure_delete = ON")
            database.execute("PRAGMA journal_mode = DELETE")
            database.execute("PRAGMA busy_timeout = 5000")
            yield database
        finally:
            database.close()


def _safe_filename(value: str) -> str:
    basename = value.replace("\\", "/").rsplit("/", 1)[-1]
    result = re.sub(r"[^A-Za-z0-9._-]+", "_", basename).strip("._")
    return (result or "artifact")[:120]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL,
  chat_id INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'uncertain')),
  attempts INTEGER NOT NULL,
  created_at_ms INTEGER NOT NULL,
  expires_at_ms INTEGER NOT NULL,
  last_error TEXT,
  UNIQUE(chat_id, turn_id)
);
CREATE TABLE IF NOT EXISTS delivery_items (
  delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('text', 'document', 'photo')),
  body TEXT,
  staging_path TEXT,
  filename TEXT,
  caption TEXT,
  size INTEGER,
  sha256 TEXT,
  status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'delivered', 'uncertain')),
  PRIMARY KEY(delivery_id, ordinal)
);
"""
