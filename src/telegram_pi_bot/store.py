"""Transactional SQLite storage for coordinator control state."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram_pi_bot.coordinator import transition as coordinate
from telegram_pi_bot.model import (
    BlockingUiState,
    BotState,
    BundleItem,
    ConversationAction,
    Effect,
    InputBundle,
    ModelRef,
    PendingSession,
    SessionConfig,
    SessionRef,
    StoredArtifact,
    Transition,
    TurnRecord,
)


SCHEMA_VERSION = 2
METADATA_RETENTION_MS = 30 * 24 * 60 * 60 * 1_000


class StoreError(RuntimeError):
    pass


class StoreConflict(StoreError):
    pass


class StoreCorrupt(StoreError):
    pass


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    effects: tuple[Effect, ...]
    replies: tuple[str, ...] = ()


class ControlStore:
    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("control database path must be absolute")
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as database:
            self._initialize(database)
            database.commit()
        os.chmod(self.path, 0o600)

    def load(self, chat_id: int) -> BotState:
        if type(chat_id) is not int or chat_id <= 0:
            raise ValueError("chat ID must be positive")
        with self._connect() as database:
            return self._load(database, chat_id)

    def claim_update(self, chat_id: int, update_id: int, now_ms: int) -> bool:
        """Persist an authorized, validated ingress identity before side effects."""
        if type(chat_id) is not int or chat_id <= 0 or type(update_id) is not int or update_id < 0 or type(now_ms) is not int or now_ms < 0:
            raise ValueError("invalid Telegram update identity")
        action_id = f"telegram_update:{update_id}"
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            if database.execute("SELECT 1 FROM actions WHERE action_id = ?", (action_id,)).fetchone():
                return False
            if database.execute("SELECT 1 FROM conversation WHERE chat_id = ?", (chat_id,)).fetchone() is None:
                self._persist_state(database, BotState(version=0, chat_id=chat_id, now_ms=now_ms))
            database.execute("INSERT INTO actions(action_id, chat_id, created_at_ms) VALUES (?, ?, ?)", (action_id, chat_id, now_ms))
            database.commit()
        return True

    def commit(self, expected_version: int, transition: Transition) -> BotState:
        if expected_version < 0:
            raise ValueError("expected version must be non-negative")
        state = transition.state
        if state.version != expected_version + 1:
            raise StoreConflict("transition version is not the next state version")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                row = database.execute(
                    "SELECT state_version FROM conversation WHERE chat_id = ?",
                    (state.chat_id,),
                ).fetchone()
                current_version = 0 if row is None else int(row[0])
                if current_version != expected_version:
                    raise StoreConflict("control state changed before commit")
                if transition.action_id is not None:
                    duplicate = database.execute(
                        "SELECT 1 FROM actions WHERE action_id = ?",
                        (transition.action_id,),
                    ).fetchone()
                    if duplicate is not None:
                        raise StoreConflict("action was already committed")
                self._persist_state(database, state)
                if transition.action_id is not None:
                    database.execute(
                        "INSERT INTO actions(action_id, chat_id, created_at_ms) VALUES (?, ?, ?)",
                        (transition.action_id, state.chat_id, state.now_ms),
                    )
                self._settle_effects(database, state.chat_id, transition)
                for effect in transition.effects:
                    if not effect.effect_id:
                        raise StoreError("committed effects require deterministic IDs")
                    database.execute(
                        """
                        INSERT INTO effects(
                          effect_id, chat_id, kind, payload_json, status, created_at_ms
                        ) VALUES (?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            effect.effect_id,
                            state.chat_id,
                            effect.kind,
                            _effect_json(effect),
                            state.now_ms,
                        ),
                    )
                database.commit()
            except BaseException:
                database.rollback()
                raise
        return state

    def recover(self, now_ms: int) -> RecoveryResult:
        if now_ms < 0:
            raise ValueError("recovery time must be non-negative")
        effects: list[Effect] = []
        replies: list[str] = []
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                chat_ids = tuple(
                    int(row[0])
                    for row in database.execute(
                        "SELECT chat_id FROM conversation ORDER BY chat_id"
                    )
                )
                for chat_id in chat_ids:
                    state = self._load(database, chat_id)
                    effect_rows = tuple(
                        database.execute(
                            """
                            SELECT effect_id, kind, payload_json, status
                            FROM effects
                            WHERE chat_id = ? AND status IN ('pending', 'claimed')
                            ORDER BY sequence
                            """,
                            (chat_id,),
                        )
                    )
                    pending = tuple(
                        _load_effect(row)
                        for row in effect_rows
                        if row["status"] == "pending"
                    )
                    claimed = tuple(
                        _load_effect(row)
                        for row in effect_rows
                        if row["status"] == "claimed"
                    )
                    action_id = f"recover:{chat_id}:{now_ms}"
                    if database.execute(
                        "SELECT 1 FROM actions WHERE action_id = ?", (action_id,)
                    ).fetchone():
                        continue
                    result = coordinate(
                        state,
                        ConversationAction(
                            "recover",
                            action_id=action_id,
                            now_ms=now_ms,
                            pending_effects=pending,
                            claimed_effects=claimed,
                        ),
                    )
                    if result.state.version == state.version:
                        continue
                    self._persist_state(database, result.state)
                    database.execute(
                        """
                        INSERT INTO actions(action_id, chat_id, created_at_ms)
                        VALUES (?, ?, ?)
                        """,
                        (action_id, chat_id, now_ms),
                    )
                    self._settle_effects(database, chat_id, result)
                    replies.extend(result.replies)
                    for effect in result.effects:
                        cursor = database.execute(
                            """
                            INSERT INTO effects(
                              effect_id, chat_id, kind, payload_json,
                              status, created_at_ms
                            ) VALUES (?, ?, ?, ?, 'pending', ?)
                            """,
                            (
                                effect.effect_id,
                                chat_id,
                                effect.kind,
                                _effect_json(effect),
                                now_ms,
                            ),
                        )
                        if cursor.rowcount == 1:
                            effects.append(effect)
                database.commit()
            except BaseException:
                database.rollback()
                raise
        return RecoveryResult(tuple(effects), tuple(replies))

    def prune(self, now_ms: int) -> int:
        if now_ms < 0:
            raise ValueError("prune time must be non-negative")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                bundle_chats = {
                    int(row[0])
                    for row in database.execute(
                        "SELECT DISTINCT chat_id FROM bundles WHERE expires_at_ms <= ?",
                        (now_ms,),
                    )
                }
                coordinated: set[int] = set()
                removed = 0
                for chat_id in bundle_chats:
                    state = self._load(database, chat_id)
                    pending = tuple(
                        _load_effect(row)
                        for row in database.execute(
                            """
                            SELECT effect_id, kind, payload_json
                            FROM effects
                            WHERE chat_id = ? AND status = 'pending'
                            ORDER BY sequence
                            """,
                            (chat_id,),
                        )
                    )
                    action_id = f"prune:{chat_id}:{now_ms}"
                    if database.execute(
                        "SELECT 1 FROM actions WHERE action_id = ?", (action_id,)
                    ).fetchone():
                        continue
                    result = coordinate(
                        state,
                        ConversationAction(
                            "prune",
                            action_id=action_id,
                            now_ms=now_ms,
                            pending_effects=pending,
                        ),
                    )
                    if result.state.version == state.version:
                        continue
                    removed += len(state.bundles) - len(result.state.bundles)
                    self._persist_state(database, result.state)
                    database.execute(
                        """
                        INSERT INTO actions(action_id, chat_id, created_at_ms)
                        VALUES (?, ?, ?)
                        """,
                        (action_id, chat_id, now_ms),
                    )
                    self._settle_effects(database, chat_id, result)
                    for effect in result.effects:
                        database.execute(
                            """
                            INSERT INTO effects(
                              effect_id, chat_id, kind, payload_json,
                              status, created_at_ms
                            ) VALUES (?, ?, ?, ?, 'pending', ?)
                            """,
                            (
                                effect.effect_id,
                                chat_id,
                                effect.kind,
                                _effect_json(effect),
                                now_ms,
                            ),
                        )
                    coordinated.add(chat_id)

                affected = {
                    int(row[0])
                    for statement in (
                        "SELECT chat_id FROM delivery WHERE expires_at_ms <= ?",
                        "SELECT chat_id FROM turns WHERE expires_at_ms <= ?",
                    )
                    for row in database.execute(statement, (now_ms,))
                }
                for table in ("delivery", "turns"):
                    cursor = database.execute(
                        f"DELETE FROM {table} WHERE expires_at_ms <= ?",  # noqa: S608
                        (now_ms,),
                    )
                    removed += cursor.rowcount
                database.execute(
                    """
                    UPDATE conversation
                    SET active_turn_id = NULL, stop_requested = 0
                    WHERE active_turn_id IS NOT NULL
                      AND active_turn_id NOT IN (SELECT turn_id FROM turns)
                    """
                )
                metadata_cutoff = now_ms - METADATA_RETENTION_MS
                database.execute(
                    "DELETE FROM actions WHERE created_at_ms <= ?", (metadata_cutoff,)
                )
                database.execute(
                    "DELETE FROM effects WHERE status != 'pending' AND created_at_ms <= ?",
                    (metadata_cutoff,),
                )
                for chat_id in affected - coordinated:
                    database.execute(
                        """
                        UPDATE conversation
                        SET state_version = state_version + 1, updated_at_ms = ?
                        WHERE chat_id = ?
                        """,
                        (now_ms, chat_id),
                    )
                database.commit()
            except BaseException:
                database.rollback()
                raise
        return removed

    def pending_effects(self, chat_id: int) -> tuple[Effect, ...]:
        with self._connect() as database:
            rows = database.execute(
                """
                SELECT effect_id, kind, payload_json
                FROM effects
                WHERE chat_id = ? AND status = 'pending'
                ORDER BY sequence
                """,
                (chat_id,),
            )
            return tuple(_load_effect(row) for row in rows)

    def mark_effect(self, effect_id: str, status: str) -> bool:
        if status not in {"done", "failed"}:
            raise ValueError("effect status is invalid")
        with self._connect() as database:
            cursor = database.execute(
                """
                UPDATE effects SET status = ?
                WHERE effect_id = ? AND status IN ('pending', 'claimed')
                """,
                (status, effect_id),
            )
            database.commit()
            return cursor.rowcount == 1

    def claim_effect(self, effect_id: str) -> bool:
        with self._connect() as database:
            cursor = database.execute(
                """
                UPDATE effects SET status = 'claimed'
                WHERE effect_id = ? AND status = 'pending'
                """,
                (effect_id,),
            )
            database.commit()
            return cursor.rowcount == 1

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        database = sqlite3.connect(self.path, timeout=5)
        try:
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA foreign_keys = ON")
            database.execute("PRAGMA secure_delete = ON")
            database.execute("PRAGMA journal_mode = DELETE")
            yield database
        except sqlite3.DatabaseError:
            raise StoreCorrupt("control database is unreadable or corrupt") from None
        finally:
            database.close()

    def _initialize(self, database: sqlite3.Connection) -> None:
        database.executescript(_SCHEMA)
        rows = tuple(database.execute("SELECT version FROM schema_meta"))
        if not rows:
            database.execute(
                "INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,)
            )
        elif len(rows) != 1 or int(rows[0][0]) != SCHEMA_VERSION:
            raise StoreCorrupt("unsupported control database schema")

    def _load(self, database: sqlite3.Connection, chat_id: int) -> BotState:
        conversation = database.execute(
            "SELECT * FROM conversation WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if conversation is None:
            return BotState(version=0, chat_id=chat_id)
        pending = {
            row["session_id"]: PendingSession(
                SessionRef("pending", row["session_id"]),
                row["name"],
                SessionConfig(
                    ModelRef(row["provider"], row["model_id"]),
                    row["thinking"],
                ),
                row["created_at_ms"],
                row["updated_at_ms"],
            )
            for row in database.execute(
                "SELECT * FROM pending_sessions WHERE chat_id = ?", (chat_id,)
            )
        }
        bundle_rows = tuple(
            database.execute("SELECT * FROM bundles WHERE chat_id = ?", (chat_id,))
        )
        bundles = {row["bundle_id"]: _load_bundle(row) for row in bundle_rows}
        current = next(
            (bundles[row["bundle_id"]] for row in bundle_rows if row["slot"] == "current"),
            None,
        )
        queued = next(
            (bundles[row["bundle_id"]] for row in bundle_rows if row["slot"] == "next"),
            None,
        )
        steering = next(
            (
                bundles[row["bundle_id"]]
                for row in bundle_rows
                if row["slot"] == "steering"
            ),
            None,
        )
        turns = {
            row["turn_id"]: TurnRecord(
                row["turn_id"],
                row["session_id"],
                row["source_message_id"],
                row["status"],
                bool(row["prompt_accepted"]),
                row["started_at_ms"],
                row["finished_at_ms"],
            )
            for row in database.execute(
                "SELECT * FROM turns WHERE chat_id = ?", (chat_id,)
            )
        }
        active_id = conversation["active_turn_id"]
        active = turns.get(active_id) if active_id is not None else None
        if active_id is not None and active is None:
            raise StoreCorrupt("active turn record is missing")
        callback = database.execute(
            """
            SELECT * FROM callbacks
            WHERE chat_id = ? AND kind = 'blocking_ui'
            ORDER BY generation DESC LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        blocking = None
        if callback is not None:
            payload = _json_object(callback["payload_json"])
            blocking = BlockingUiState(
                _required_text(payload.get("request_id"), "blocking UI request"),
                _required_text(payload.get("turn_id"), "blocking UI turn"),
                callback["expires_at_ms"],
                callback["callback_key"],
                callback["generation"],
            )
        artifacts = {
            row["delivery_id"]: StoredArtifact(
                row["delivery_id"],
                row["staging_path"],
                row["sha256"],
                row["size"],
                row["status"],
                row["expires_at_ms"],
            )
            for row in database.execute(
                "SELECT * FROM delivery WHERE chat_id = ?", (chat_id,)
            )
        }
        return BotState(
            version=conversation["state_version"],
            chat_id=chat_id,
            now_ms=conversation["updated_at_ms"],
            selected_session_id=conversation["selected_session_id"],
            selected_session_path=conversation["selected_session_path"],
            pending_sessions=pending,
            bundle=current,
            next_bundle=queued,
            steering_bundle=steering,
            active_turn=active,
            active_turn_id=active_id,
            blocking_ui=blocking,
            progress_message_id=conversation["progress_message_id"],
            callback_generation=conversation["callback_generation"],
            stop_requested=bool(conversation["stop_requested"]),
            bundles=bundles,
            turns=turns,
            artifacts=artifacts,
        )

    def _persist_state(self, database: sqlite3.Connection, state: BotState) -> None:
        database.execute(
            """
            INSERT INTO conversation(
              chat_id, state_version, selected_session_id, selected_session_path,
              active_turn_id, progress_message_id, callback_generation,
              stop_requested, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
              state_version = excluded.state_version,
              selected_session_id = excluded.selected_session_id,
              selected_session_path = excluded.selected_session_path,
              active_turn_id = excluded.active_turn_id,
              progress_message_id = excluded.progress_message_id,
              callback_generation = excluded.callback_generation,
              stop_requested = excluded.stop_requested,
              updated_at_ms = excluded.updated_at_ms
            """,
            (
                state.chat_id,
                state.version,
                state.selected_session_id,
                state.selected_session_path,
                state.active_turn_id,
                state.progress_message_id,
                state.callback_generation,
                int(state.stop_requested),
                state.now_ms,
            ),
        )
        database.execute("DELETE FROM pending_sessions WHERE chat_id = ?", (state.chat_id,))
        database.executemany(
            """
            INSERT INTO pending_sessions(
              session_id, chat_id, name, provider, model_id, thinking,
              created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    item.session_id,
                    state.chat_id,
                    item.name,
                    item.provider,
                    item.model_id,
                    item.thinking,
                    item.created_at_ms,
                    item.updated_at_ms,
                )
                for item in state.pending_sessions.values()
            ),
        )
        database.execute("DELETE FROM bundles WHERE chat_id = ?", (state.chat_id,))
        for bundle in state.bundles.values():
            slot = "stored"
            if state.bundle is not None and bundle.bundle_id == state.bundle.bundle_id:
                slot = "current"
            elif state.next_bundle is not None and bundle.bundle_id == state.next_bundle.bundle_id:
                slot = "next"
            elif (
                state.steering_bundle is not None
                and bundle.bundle_id == state.steering_bundle.bundle_id
            ):
                slot = "steering"
            database.execute(
                """
                INSERT INTO bundles(
                  bundle_id, chat_id, slot, status, due_at_ms, content_json,
                  created_at_ms, expires_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle.bundle_id,
                    state.chat_id,
                    slot,
                    bundle.status,
                    bundle.due_at_ms,
                    _bundle_json(bundle),
                    bundle.created_at_ms,
                    bundle.expires_at_ms,
                ),
            )

        database.execute("DELETE FROM turns WHERE chat_id = ?", (state.chat_id,))
        for turn in state.turns.values():
            finished = turn.finished_at_ms
            expires = (
                finished + METADATA_RETENTION_MS
                if finished is not None
                else 2**63 - 1
            )
            database.execute(
                """
                INSERT INTO turns(
                  turn_id, chat_id, session_id, source_message_id, status,
                  prompt_accepted, started_at_ms, finished_at_ms, expires_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.turn_id,
                    state.chat_id,
                    turn.session_id,
                    turn.source_message_id,
                    turn.status,
                    int(turn.prompt_accepted),
                    turn.started_at_ms,
                    turn.finished_at_ms,
                    expires,
                ),
            )
        database.execute("DELETE FROM callbacks WHERE chat_id = ?", (state.chat_id,))
        if state.blocking_ui is not None:
            database.execute(
                """
                INSERT INTO callbacks(
                  callback_key, chat_id, generation, kind, payload_json, expires_at_ms
                ) VALUES (?, ?, ?, 'blocking_ui', ?, ?)
                """,
                (
                    state.blocking_ui.callback_key,
                    state.chat_id,
                    state.blocking_ui.generation,
                    _json_dump(
                        {
                            "request_id": state.blocking_ui.request_id,
                            "turn_id": state.blocking_ui.turn_id,
                        }
                    ),
                    state.blocking_ui.expires_at_ms,
                ),
            )
        database.execute("DELETE FROM delivery WHERE chat_id = ?", (state.chat_id,))
        for artifact in state.artifacts.values():
            database.execute(
                """
                INSERT INTO delivery(
                  delivery_id, chat_id, turn_id, kind, staging_path, sha256,
                  size, status, expires_at_ms
                ) VALUES (?, ?, NULL, 'artifact', ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    state.chat_id,
                    artifact.staging_path,
                    artifact.sha256,
                    artifact.size,
                    artifact.status,
                    artifact.expires_at_ms,
                ),
            )

    def _settle_effects(
        self,
        database: sqlite3.Connection,
        chat_id: int,
        transition: Transition,
    ) -> None:
        for effect_id, status in transition.settled_effects.items():
            cursor = database.execute(
                """
                UPDATE effects SET status = ?
                WHERE effect_id = ? AND chat_id = ?
                  AND status IN ('pending', 'claimed')
                """,
                (status, effect_id, chat_id),
            )
            if cursor.rowcount != 1:
                raise StoreConflict("effect completion is stale or unknown")


def _bundle_json(bundle: InputBundle) -> str:
    return _json_dump(
        {
            "kind": bundle.kind,
            "timer_generation": bundle.timer_generation,
            "held": bundle.held,
            "items": [
                {
                    "kind": item.kind,
                    "value": item.value,
                    "source_message_id": item.source_message_id,
                }
                for item in bundle.items
            ],
        }
    )


def _load_bundle(row: sqlite3.Row) -> InputBundle:
    payload = _json_object(row["content_json"])
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise StoreCorrupt("bundle items are invalid")
    items = tuple(
        BundleItem(
            _required_text(item.get("kind"), "bundle item kind"),
            _required_text(item.get("value"), "bundle item value"),
            _required_int(item.get("source_message_id"), "source message ID"),
        )
        for item in raw_items
        if isinstance(item, dict)
    )
    if len(items) != len(raw_items):
        raise StoreCorrupt("bundle item shape is invalid")
    return InputBundle(
        row["bundle_id"],
        _required_text(payload.get("kind"), "bundle kind"),
        row["status"],
        items,
        row["due_at_ms"],
        _required_int(payload.get("timer_generation"), "timer generation"),
        row["created_at_ms"],
        row["expires_at_ms"],
        payload.get("held") is True,
    )


def _effect_json(effect: Effect) -> str:
    return _json_dump({"value": effect.value, "payload": dict(effect.payload)})


def _load_effect(row: sqlite3.Row) -> Effect:
    value = _json_object(row["payload_json"])
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise StoreCorrupt("effect payload is invalid")
    return Effect(
        row["kind"],
        value.get("value"),
        row["effect_id"],
        payload,
    )


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise StoreCorrupt("stored JSON is invalid") from error
    if not isinstance(parsed, dict):
        raise StoreCorrupt("stored JSON object is invalid")
    return parsed


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StoreCorrupt(f"{label} is invalid")
    return value


def _required_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise StoreCorrupt(f"{label} is invalid")
    return value


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS conversation (
  chat_id INTEGER PRIMARY KEY,
  state_version INTEGER NOT NULL,
  selected_session_id TEXT,
  selected_session_path TEXT,
  active_turn_id TEXT,
  progress_message_id INTEGER,
  callback_generation INTEGER NOT NULL,
  stop_requested INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_sessions (
  session_id TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL REFERENCES conversation(chat_id) ON DELETE CASCADE,
  name TEXT,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  thinking TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bundles (
  bundle_id TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL REFERENCES conversation(chat_id) ON DELETE CASCADE,
  slot TEXT NOT NULL,
  status TEXT NOT NULL,
  due_at_ms INTEGER,
  content_json TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  expires_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS effects (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  effect_id TEXT NOT NULL UNIQUE,
  chat_id INTEGER NOT NULL REFERENCES conversation(chat_id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
  turn_id TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL REFERENCES conversation(chat_id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  prompt_accepted INTEGER NOT NULL,
  started_at_ms INTEGER NOT NULL,
  finished_at_ms INTEGER,
  expires_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS callbacks (
  callback_key TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL REFERENCES conversation(chat_id) ON DELETE CASCADE,
  generation INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  expires_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery (
  delivery_id TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL REFERENCES conversation(chat_id) ON DELETE CASCADE,
  turn_id TEXT,
  kind TEXT NOT NULL,
  staging_path TEXT,
  sha256 TEXT,
  size INTEGER,
  status TEXT NOT NULL,
  expires_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL REFERENCES conversation(chat_id) ON DELETE CASCADE,
  created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS bundles_chat ON bundles(chat_id);
CREATE INDEX IF NOT EXISTS effects_pending ON effects(chat_id, status, created_at_ms);
CREATE INDEX IF NOT EXISTS turns_chat ON turns(chat_id);
CREATE INDEX IF NOT EXISTS callbacks_chat ON callbacks(chat_id, generation);
CREATE INDEX IF NOT EXISTS delivery_chat ON delivery(chat_id);
"""
