"""Composition root for the private Telegram Pi bot."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import logging
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

from telegram_pi_bot.config import BotConfig
from telegram_pi_bot.artifact_ipc import ArtifactPolicy, sweep_artifacts
from telegram_pi_bot.coordinator import INPUT_ACTION_KINDS, transition
from telegram_pi_bot.effects import Clock, EffectRunner, SystemClock
from telegram_pi_bot.doctor import Doctor, validate_storage_paths
from telegram_pi_bot.media import Attachment, AttachmentPolicy, AttachmentStore, GroqTranscriber, MediaError
from telegram_pi_bot.model import ConversationAction, NativeSessionRef
from telegram_pi_bot.outbound import DeliveryQueue
from telegram_pi_bot.pi_runtime import PiRuntime
from telegram_pi_bot.store import ControlStore, StoreConflict
from telegram_pi_bot.telegram_adapter import TelegramAdapter, TelegramDownloadError
from telegram_pi_bot.telegram_ui import BOT_COMMANDS, ProgressRenderer, callback_data, split_final_text


HELP_TEXT = (
    "Send text, voice, photos, or documents. Text waits 5 seconds and media "
    "waits 10 seconds so nearby messages can form one request. 👀 means accepted, "
    "👌 means delivered, and 😨 means failed or uncertain. New work queues while "
    "Pi is busy; use Send or Steer explicitly. Use Pi sessions sequentially with "
    "the terminal; the bot cannot lock terminal Pi. Generic blocking select/confirm "
    "buttons expire after 10 minutes; input/editor requests are cancelled.\n\n"
    + "\n".join(f"/{entry.command} — {entry.description}" for entry in BOT_COMMANDS)
)
SWEEP_INTERVAL_MS = 60 * 60 * 1_000


class BotApplication:
    def __init__(
        self,
        *,
        config: BotConfig,
        store: ControlStore,
        runtime: Any,
        telegram: Any,
        attachment_store: AttachmentStore,
        transcriber: GroqTranscriber,
        delivery: DeliveryQueue,
        effects: EffectRunner,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.runtime = runtime
        self.telegram = telegram
        self.attachment_store = attachment_store
        self.transcriber = transcriber
        self.delivery = delivery
        self.effects = effects
        self.clock = clock or SystemClock()
        self._transition_lock = asyncio.Lock()
        self._drain_lock = asyncio.Lock()
        self._attachments: dict[str, Attachment] = {}
        self._bundle_controls: dict[str, tuple[int, tuple[int, str]]] = {}
        self._controls_lock = asyncio.Lock()
        self._session_listing: tuple[str, ...] = ()
        self._catalog_choices: dict[str, tuple[str, str, str | None]] = {}
        self._catalog_generation = 0
        self._sweeper: asyncio.Task | None = None
        self.poller_lock = None
        self.validate_readiness = False
        self.doctor = Doctor(config, runtime, telegram=telegram, delivery=delivery)
        self._started = False
        effects.bind(self.dispatch)
        effects.attachments = self._attachments

    @classmethod
    def from_config(cls, config: BotConfig) -> BotApplication:
        validate_storage_paths(config)
        holder: dict[str, BotApplication] = {}

        async def dispatch(action: ConversationAction) -> None:
            await holder["app"].dispatch(action)

        attachment_store = AttachmentStore(AttachmentPolicy.from_config(config))
        store = ControlStore(config.state_dir / "control.sqlite3")
        runtime = PiRuntime(
            config,
            artifact_policy=ArtifactPolicy.from_config(config),
            image_loader=attachment_store.read_photo,
        )
        telegram = TelegramAdapter(config, dispatch, claim_update=lambda update_id, now: store.claim_update(config.allowed_user_id, update_id, now))
        delivery = DeliveryQueue(
            config.state_dir / "delivery.sqlite3",
            config.allowed_user_id,
            staging_root=config.state_dir / "artifacts" / "staging",
            max_artifacts=config.outbound_artifacts_per_turn,
            max_artifact_bytes=config.outbound_total_bytes,
            pending_retention_ms=config.staging_retention_seconds * 1_000,
            metadata_retention_ms=config.metadata_retention_seconds * 1_000,
        )
        delivery.staging_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        delivery.staging_root.mkdir(mode=0o700, exist_ok=True)
        progress = ProgressRenderer(telegram)
        effects = EffectRunner(
            chat_id=config.allowed_user_id,
            store=store,
            runtime=runtime,
            telegram=telegram,
            delivery=delivery,
            progress=progress,
            attachment_store=attachment_store,
        )
        app = cls(
            config=config,
            store=store,
            runtime=runtime,
            telegram=telegram,
            attachment_store=attachment_store,
            transcriber=GroqTranscriber(config.groq_key_file),
            delivery=delivery,
            effects=effects,
        )
        holder["app"] = app
        app.validate_readiness = True
        return app

    async def start(self) -> None:
        if self._started:
            return
        try:
            if self.validate_readiness:
                self.doctor.poller_lock = self.poller_lock
                failed = [check.name for check in await self.doctor.run() if not check.ok]
                if failed:
                    raise RuntimeError("readiness failed: " + ", ".join(failed))
            self._restore_attachments()
            await self.sweep(drain=False)
            await self.recover()
        except BaseException:
            await self.effects.close()
            raise
        self._started = True
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="pi-retention-sweeper")

    async def recover(self) -> None:
        recovery = self.store.recover(self.clock.now_ms())
        for reply in recovery.replies:
            await self._safe_text(reply)
        await self._drain()
        for delivery_id in self.delivery.pending_delivery_ids():
            self.effects.resume_delivery(delivery_id)
        for delivery_id in self.delivery.expired_delivery_ids():
            await self.effects.notice_expired_delivery(delivery_id)
        await self._show_bundle_controls()

    async def drain_effects(self) -> bool:
        await self._drain()
        return not self.store.pending_effects(self.config.allowed_user_id)

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            if self._sweeper is not None:
                self._sweeper.cancel()
                await asyncio.gather(self._sweeper, return_exceptions=True)
                self._sweeper = None
            state = self.store.load(self.config.allowed_user_id)
            if state.active_turn is not None:
                await self.dispatch(
                    ConversationAction("stop", now_ms=self.clock.now_ms())
                )
            await self.effects.close()
            state = self.store.load(self.config.allowed_user_id)
            if state.active_turn is not None and state.active_turn.status in {
                "configuring",
                "compacting",
            }:
                recovery = self.store.recover(self.clock.now_ms())
                for reply in recovery.replies:
                    await self._safe_text(reply)
        finally:
            self._started = False

    async def sweep(self, *, drain: bool = True) -> None:
        async with self._transition_lock:
            self.store.prune(self.clock.now_ms())
            live = self.store.load(self.config.allowed_user_id)
            protected = {
                item.value for bundle in live.bundles.values() for item in bundle.items
                if item.kind in {"photo", "document"}
            }
            # Accepted turns have already removed their prompt bodies from SQLite.
            protected.update(self.effects.active_attachment_paths())
            for value, attachment in tuple(self._attachments.items()):
                if value not in protected:
                    try:
                        self.attachment_store.release(attachment)
                    except (OSError, MediaError):
                        pass
                    self._attachments.pop(value, None)
            self.attachment_store.sweep(self.clock.now_ms(), protected=map(Path, protected))
            self.delivery.sweep(self.clock.now_ms())
            sweep_artifacts(ArtifactPolicy.from_config(self.config), now_seconds=self.clock.now_ms() / 1_000)
        if drain:
            await self._drain()
            for delivery_id in self.delivery.expired_delivery_ids():
                await self.effects.notice_expired_delivery(delivery_id)
            await self._show_bundle_controls()

    async def _sweep_loop(self) -> None:
        while True:
            await self.clock.sleep_until(self.clock.now_ms() + SWEEP_INTERVAL_MS)
            try:
                await self.sweep()
            except Exception:
                logging.getLogger(__name__).warning("Retention cleanup failed; retrying at the next interval.")

    async def dispatch(self, action: ConversationAction) -> None:
        if action.kind.startswith("command_"):
            await self._command(action)
            return
        if action.kind == "telegram_callback":
            await self._callback(action)
            return
        if action.kind in {"telegram_voice", "telegram_photo", "telegram_document"}:
            await self._media(action)
            return
        removed_attachments: tuple[Attachment, ...] = ()
        async with self._transition_lock:
            state = self.store.load(self.config.allowed_user_id)
            if action.kind in INPUT_ACTION_KINDS and state.selected_session_id is None:
                pending = self.effects.new_pending_session()
                created = transition(
                    state,
                    ConversationAction(
                        "new_session",
                        session_id=pending.ref.id,
                        provider=pending.provider,
                        model_id=pending.model_id,
                        thinking=pending.thinking,
                        now_ms=action.get("now_ms", self.clock.now_ms()),
                    ),
                )
                if created.state.version != state.version:
                    state = self.store.commit(state.version, created)
            old_bundle = state.bundles.get(action.get("bundle_id"))
            result = transition(state, action)
            if result.state.version != state.version:
                try:
                    self.store.commit(state.version, result)
                except StoreConflict:
                    return
            if action.kind == "session_operation_completed":
                self._catalog_choices.clear()
            if action.kind == "cancel_bundle" and old_bundle is not None:
                removed_attachments = tuple(
                    self._attachments.pop(item.value)
                    for item in old_bundle.items
                    if item.value in self._attachments
                )
        for attachment in removed_attachments:
            try:
                self.attachment_store.release(attachment)
            except MediaError:
                pass
        for reply in result.replies:
            await self._safe_text(reply)
        await self._drain()
        if action.kind in INPUT_ACTION_KINDS | {
            "stop",
            "bundle_dispatched",
            "turn_completed",
        }:
            await self._show_bundle_controls()
        await asyncio.sleep(0)

    async def _drain(self) -> None:
        while True:
            claimed_effect = None
            async with self._drain_lock:
                pending = self.store.pending_effects(self.config.allowed_user_id)
                for effect in pending:
                    if self.store.claim_effect(effect.effect_id):
                        claimed_effect = effect
                        break
            if claimed_effect is None:
                return
            await self.effects.run(claimed_effect)

    async def _media(self, action: ConversationAction) -> None:
        kind = action.kind.removeprefix("telegram_")
        file_id = action.get("telegram_file_id")
        file_size = action.get("file_size")
        if not isinstance(file_id, str):
            return
        existing = self._bundle_attachments()
        limit = {
            "voice": self.config.voice_bytes,
            "photo": self.config.photo_bytes,
            "document": self.config.document_bytes,
        }[kind]
        attachment: Attachment | None = None
        try:
            attachment = await self.attachment_store.stage(
                kind=kind,
                telegram_file_id=file_id,
                filename=action.get("filename"),
                mime_type=action.get("mime_type"),
                expected_size=file_size,
                chunks=self.telegram.download_file(file_id, limit),
                existing=existing,
                now_ms=action.get("now_ms", self.clock.now_ms()),
            )
            if kind == "voice":
                value = await self.transcriber.transcribe(attachment.path)
                self.attachment_store.release(attachment)
            else:
                value = str(attachment.path)
                self._attachments[value] = attachment
        except (MediaError, TelegramDownloadError) as error:
            if attachment is not None:
                try:
                    self.attachment_store.release(attachment)
                except MediaError:
                    pass
            source_message_id = action.get("source_message_id")
            if type(source_message_id) is int and source_message_id > 0:
                try:
                    await self.telegram.react(
                        self.config.allowed_user_id, source_message_id, "😨"
                    )
                except Exception:
                    pass
            await self._safe_text(str(error))
            return
        await self.dispatch(
            ConversationAction(
                f"add_{kind}",
                attachment_id=value,
                source_message_id=action.get("source_message_id"),
                update_id=action.get("update_id"),
                now_ms=action.get("now_ms", self.clock.now_ms()),
            )
        )

    def _bundle_attachments(self) -> tuple[Attachment, ...]:
        state = self.store.load(self.config.allowed_user_id)
        paths = {
            item.value
            for bundle in state.bundles.values()
            for item in bundle.items
            if item.kind in {"photo", "document"}
        }
        return tuple(
            attachment
            for path, attachment in self._attachments.items()
            if path in paths
        )

    def _restore_attachments(self) -> None:
        state = self.store.load(self.config.allowed_user_id)
        for bundle in state.bundles.values():
            for item in bundle.items:
                if item.kind not in {"photo", "document"}:
                    continue
                attachment = self._owned_attachment(item.kind, item.value)
                if attachment is not None:
                    self._attachments[item.value] = attachment

    def _owned_attachment(self, kind: str, value: str) -> Attachment | None:
        path = Path(value).absolute()
        root = self.attachment_store.policy.root.absolute()
        if (
            path.parent != root
            or not re.fullmatch(r"[0-9a-f]{32}(?:\.[a-z0-9]{1,12})?", path.name)
        ):
            return None
        try:
            details = path.lstat()
            limit = self.config.photo_bytes if kind == "photo" else self.config.document_bytes
            if not stat.S_ISREG(details.st_mode) or not 0 < details.st_size <= limit:
                return None
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    digest.update(chunk)
        except OSError:
            return None
        return Attachment(
            attachment_id=path.stem,
            telegram_file_id="recovered",
            kind=kind,
            display_name=path.name,
            path=path,
            size_bytes=details.st_size,
            sha256=digest.hexdigest(),
            mime_type="",
            created_at_ms=details.st_mtime_ns // 1_000_000,
        )

    async def _callback(self, action: ConversationAction) -> None:
        callback_action = action.get("callback_action")
        key = action.get("callback_key")
        generation = action.get("generation")
        state = self.store.load(self.config.allowed_user_id)
        if not isinstance(callback_action, str) or not isinstance(key, str):
            return
        if callback_action.startswith("catalog_"):
            choice = self._catalog_choices.get(key)
            if choice is None or generation != self._catalog_generation:
                return
            kind, selector, session_id = choice
            if session_id != state.selected_session_id:
                return
            if kind == "page":
                catalog_kind, page = selector.split(":")
                await self._show_catalog(catalog_kind, state, page=int(page))
                return
            if kind == "skill":
                try:
                    skill = await self.effects.resolve_skill(selector)
                except Exception:
                    await self._safe_text("That skill is not available.")
                    return
                await self._safe_text(f"Invoke with /skill {skill.name} <request>.")
                return
            self._catalog_choices.clear()
            await self._command(ConversationAction(kind, selector=selector, update_id=action.get("update_id"), now_ms=action.get("now_ms", self.clock.now_ms())))
            return
        if callback_action.startswith("ui_"):
            blocking = state.blocking_ui
            if (
                blocking is None
                or blocking.callback_key != key
                or blocking.generation != generation
            ):
                return
            response = self.effects.ui_response(callback_action, key, generation)
            if response is None and action.get("response") is not None:
                response = action.get("response")
            if response is None:
                return
            await self.dispatch(
                ConversationAction(
                    "answer_ui",
                    callback_key=key,
                    generation=generation,
                    response=response,
                    callback_query_id=action.get("callback_query_id"),
                    now_ms=action.get("now_ms", self.clock.now_ms()),
                )
            )
            return
        matches = [
            bundle
            for bundle in state.bundles.values()
            if _opaque_key(bundle.bundle_id) == key
            and bundle.timer_generation == generation
        ]
        if len(matches) != 1:
            return
        kind = {
            "send": "send_now",
            "cancel": "cancel_bundle",
            "steer": "steer_current",
        }.get(callback_action)
        if kind is None:
            return
        await self.dispatch(
            ConversationAction(
                kind,
                bundle_id=matches[0].bundle_id,
                callback_query_id=action.get("callback_query_id"),
                now_ms=action.get("now_ms", self.clock.now_ms()),
            )
        )

    async def _show_bundle_controls(self) -> None:
        async with self._controls_lock:
            await self._update_bundle_controls()

    async def _update_bundle_controls(self) -> None:
        state = self.store.load(self.config.allowed_user_id)
        live_by_id = {
            bundle.bundle_id: bundle
            for bundle in state.bundles.values()
            if bundle.status == "frozen"
        }
        live_by_id.update(
            {
                bundle.bundle_id: bundle
                for bundle in (state.bundle, state.next_bundle)
                if bundle is not None
                and bundle.status in {"open", "queued", "frozen"}
            }
        )
        live = tuple(live_by_id.values())
        for bundle_id, (message_id, _identity) in tuple(self._bundle_controls.items()):
            if bundle_id not in live_by_id:
                try:
                    await self.telegram.edit_choices(self.config.allowed_user_id, message_id, "Input is no longer pending.", ())
                except Exception:
                    continue
                self._bundle_controls.pop(bundle_id, None)
        for bundle in live:
            identity = (bundle.timer_generation, bundle.status)
            previous = self._bundle_controls.get(bundle.bundle_id)
            if previous is not None and previous[1] == identity:
                continue
            key = _opaque_key(bundle.bundle_id)
            choices = [
                (
                    "Send now",
                    callback_data("send", key, bundle.timer_generation),
                )
            ]
            if (
                bundle.status == "queued"
                and state.next_bundle is not None
                and state.next_bundle.bundle_id == bundle.bundle_id
                and state.active_turn is not None
                and state.active_turn.status == "active"
            ):
                choices.append(
                    (
                        "Steer current",
                        callback_data("steer", key, bundle.timer_generation),
                    )
                )
            choices.append(
                (
                    "Cancel",
                    callback_data("cancel", key, bundle.timer_generation),
                )
            )
            try:
                text = f"{len(bundle.items)} input item(s), {bundle.status}. Choose how Pi should handle it."
                if previous is None:
                    message_id = await self.telegram.send_choices(self.config.allowed_user_id, text, tuple(choices))
                else:
                    message_id = previous[0]
                    await self.telegram.edit_choices(self.config.allowed_user_id, message_id, text, tuple(choices))
            except Exception:
                continue
            self._bundle_controls[bundle.bundle_id] = (message_id, identity)

    async def _command(self, action: ConversationAction) -> None:
        state = self.store.load(self.config.allowed_user_id)
        now_ms = action.get("now_ms", self.clock.now_ms())
        if action.kind == "command_help":
            await self._safe_text(HELP_TEXT)
            return
        if action.kind == "command_new":
            if state.active_turn is not None:
                await self._safe_text("Wait for the active Pi operation to finish before changing sessions.")
                return
            pending = self.effects.new_pending_session(action.get("name"))
            await self.dispatch(
                ConversationAction(
                    "new_session",
                    session_id=pending.ref.id,
                    name=pending.name,
                    provider=pending.provider,
                    model_id=pending.model_id,
                    thinking=pending.thinking,
                    update_id=action.get("update_id"),
                    now_ms=now_ms,
                )
            )
            selected = self.store.load(self.config.allowed_user_id)
            if selected.selected_session_id == pending.ref.id:
                self._catalog_choices.clear()
                await self._safe_text(f"New pending session: {pending.ref.id}; not terminal-resumable until its first model turn.")
            return
        if action.kind == "command_skill":
            name = action.get("name")
            request = action.get("request")
            try:
                skill = await self.effects.resolve_skill(name)
            except Exception:
                await self._safe_text("That skill is not available.")
                return
            await self.dispatch(
                ConversationAction(
                    "add_text",
                    text=f"/skill:{skill.name} {request}",
                    source_message_id=action.get("source_message_id"),
                    update_id=action.get("update_id"),
                    now_ms=now_ms,
                )
            )
            return
        if action.kind == "stop":
            await self.dispatch(
                ConversationAction(
                    "stop",
                    update_id=action.get("update_id"),
                    now_ms=now_ms,
                )
            )
            return
        if action.kind == "command_sessions":
            self._session_listing = ()
            try:
                sessions = await self.effects.list_sessions(action.get("limit", 10))
            except Exception:
                await self._safe_text("Native session listing is unavailable.")
                return
            state = self.store.load(self.config.allowed_user_id)
            pending = sorted(state.pending_sessions.values(), key=lambda item: (item.updated_at_ms, item.ref.id), reverse=True)
            entries = [item.ref.id for item in pending]
            entries.extend(item.ref.id for item in sessions if item.ref.id not in state.pending_sessions)
            lines = [f"pending {item.ref.id} {item.name or ''} — not terminal-resumable" for item in pending]
            lines.extend(f"native {item.ref.id} {item.name or ''}".rstrip() for item in sessions if item.ref.id not in state.pending_sessions)
            text = "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1)) or "No sessions found."
            if await self._safe_text(text):
                self._session_listing = tuple(entries)
            return
        if action.kind == "command_use":
            await self._use_session(action.selector, now_ms, action)
            return
        if action.kind in {"command_model_catalog", "command_thinking_catalog", "command_skill_catalog", "command_status", "command_usage", "command_doctor"}:
            await self._show_catalog(action.kind, state)
            return
        if action.kind in {"command_model", "command_thinking"}:
            self._catalog_choices.clear()
            await self._start_session_operation(
                action,
                operation_kind="configure",
                mode="model" if action.kind == "command_model" else "thinking",
            )
            return
        if action.kind == "command_compact":
            await self._start_session_operation(
                action,
                operation_kind="compact",
                instructions=action.get("instructions"),
            )

    async def _show_catalog(self, kind: str, state, *, page: int = 0) -> None:
        if kind == "command_doctor":
            self.doctor.poller_lock = self.poller_lock
            checks = await self.doctor.run()
            await self._safe_text("\n".join(f"{check.name}: {'OK' if check.ok else 'FAIL'} — {check.detail}" for check in checks))
            return
        is_catalog = kind in {"command_model_catalog", "command_thinking_catalog", "command_skill_catalog"}
        if is_catalog:
            self._catalog_choices.clear()
            self._catalog_generation += 1
        session = NativeSessionRef(state.selected_session_id) if state.selected_session_path else None
        pending = state.pending_sessions.get(state.selected_session_id)
        try:
            snapshot = await self.effects.inspect(session, config=pending.config if pending else None)
        except Exception:
            await self._safe_text("Pi metadata is unavailable.")
            return
        if is_catalog:
            if kind == "command_model_catalog":
                labels = {"local": "Local", "ollama_cloud": "Ollama Cloud", "remote": "Remote"}
                entries = [(f"{item.provider}/{item.model_id} — {labels.get(item.location, 'Remote')}", "command_model", f"{item.provider}/{item.model_id}") for item in snapshot.models]
            elif kind == "command_thinking_catalog":
                entries = [(level, "command_thinking", level) for level in snapshot.thinking_levels]
            else:
                entries = [(item.name, "skill", item.name) for item in snapshot.skills]
            selected = entries[page * 10:(page + 1) * 10]
            choices = []
            for label, command_kind, selector in selected:
                key = secrets.token_hex(8)
                self._catalog_choices[key] = (command_kind, selector, state.selected_session_id)
                choices.append((label[:64], callback_data("catalog_pick", key, self._catalog_generation)))
            for label, target in (("Previous", page - 1), ("Next", page + 1)):
                if 0 <= target and target * 10 < len(entries):
                    key = secrets.token_hex(8)
                    self._catalog_choices[key] = ("page", f"{kind}:{target}", state.selected_session_id)
                    choices.append((label, callback_data("catalog_page", key, self._catalog_generation)))
            if not choices:
                await self._safe_text("No entries found.")
                return
            text = "\n".join(label for label, _command, _selector in selected)
            if kind == "command_skill_catalog":
                text = "\n".join(f"{item.name} — {item.description}".rstrip(" —") for item in snapshot.skills[page * 10:(page + 1) * 10])
            try:
                await self.telegram.send_choices(self.config.allowed_user_id, text[:4096], tuple(choices))
            except Exception:
                self._catalog_choices.clear()
            return
        elif kind == "command_usage":
            stats = dict(snapshot.session_stats)
            stats.setdefault("compactions", "unreported by Pi")
            text = "\n".join(f"{key}: {value if value is not None else 'unreported by Pi'}" for key, value in stats.items())
        else:
            model = pending.config.model if pending else snapshot.selected_model
            thinking = pending.thinking if pending else snapshot.thinking
            name = pending.name if pending else snapshot.session_name
            text = (
                f"session: {state.selected_session_id or 'none'} {name or ''} {'pending (not terminal-resumable)' if pending else 'native' if session else ''}\n"
                f"model: {model.provider + '/' + model.model_id if model else 'unknown'}\n"
                f"thinking: {thinking or 'unknown'}\n"
                f"context: {snapshot.session_stats.get('contextUsage.tokens', 'unreported')} tokens; "
                f"{snapshot.session_stats.get('contextUsage.percent', 'unreported')}% of "
                f"{snapshot.session_stats.get('contextUsage.contextWindow', 'unreported')}\n"
                f"turn: {state.active_turn.status if state.active_turn else 'idle'}\n"
                f"queued: {len(state.next_bundle.items) if state.next_bundle else 0}\n"
                f"blocking UI: {'waiting' if state.blocking_ui else 'none'}\n"
                f"delivery: {self.delivery.latest_status() or 'none'}"
            )
        await self._safe_text(text or "No entries found.")

    async def _use_session(self, selector: str | None, now_ms: int, source: ConversationAction) -> None:
        if not isinstance(selector, str):
            return
        state = self.store.load(self.config.allowed_user_id)
        if state.active_turn is not None:
            await self._safe_text("Wait for the active Pi operation to finish before changing sessions.")
            return
        if selector.isdecimal():
            index = int(selector) - 1
            if not 0 <= index < len(self._session_listing):
                await self._safe_text("Session number is stale; run /sessions again.")
                return
            selector = self._session_listing[index]
        matches: list[tuple[str, str | None]] = []
        for item in state.pending_sessions.values():
            if item.ref.id.startswith(selector):
                matches.append((item.ref.id, None))
        try:
            sessions = await self.effects.list_sessions(None)
        except Exception:
            await self._safe_text("Session selection could not be validated.")
            return
        for item in sessions:
            if item.ref.id.startswith(selector) and item.ref.id not in state.pending_sessions:
                matches.append((item.ref.id, item.ref.id))
        if len(matches) != 1:
            await self._safe_text("Session selector must match exactly one session.")
            return
        session_id, native_path = matches[0]
        await self.dispatch(
            ConversationAction(
                "use_session",
                session_id=session_id,
                native_path=native_path,
                update_id=source.get("update_id"),
                now_ms=now_ms,
            )
        )
        selected = self.store.load(self.config.allowed_user_id)
        if selected.selected_session_id == session_id:
            self._catalog_choices.clear()
            await self._safe_text(f"Using session: {session_id}. Share sequentially with terminal Pi.")

    async def _start_session_operation(
        self,
        action: ConversationAction,
        *,
        operation_kind: str,
        mode: str | None = None,
        instructions: str | None = None,
    ) -> None:
        state = self.store.load(self.config.allowed_user_id)
        session_id = state.selected_session_id
        if session_id is None:
            await self._safe_text("Create or select a session first.")
            return
        update_id = action.get("update_id")
        if type(update_id) is not int or update_id < 0:
            await self._safe_text("Telegram command identity is invalid.")
            return
        operation_id = f"session-operation-{operation_kind}-{update_id}"
        try:
            self.effects.stage_session_operation(
                operation_id,
                kind=operation_kind,
                mode=mode,
                selector=action.selector,
                instructions=instructions,
                agy_provider=self.config.agy_provider,
                agy_model_id=self.config.agy_model_id,
                agy_thinking=self.config.agy_thinking,
            )
        except ValueError:
            return
        await self.dispatch(
            ConversationAction(
                "begin_session_operation",
                operation_id=operation_id,
                operation_kind=operation_kind,
                session_id=session_id,
                update_id=update_id,
                now_ms=action.get("now_ms", self.clock.now_ms()),
            )
        )
        active = self.store.load(self.config.allowed_user_id).active_turn
        if active is None or active.turn_id != operation_id:
            self.effects.discard_session_operation(operation_id)

    async def _safe_text(self, text: str) -> bool:
        try:
            for part in split_final_text(text):
                await self.telegram.send_text(self.config.allowed_user_id, part)
        except Exception:
            return False
        return True


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor = -1

    @property
    def held(self) -> bool:
        return self._descriptor >= 0

    def acquire(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            raise RuntimeError("the telegram-pi-bot process lock is unsafe or unavailable") from None
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
                raise OSError
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(descriptor, 0)
            payload = f"{os.getpid()}\n".encode("ascii")
            if os.write(descriptor, payload) != len(payload):
                raise OSError
            os.fsync(descriptor)
        except BlockingIOError:
            os.close(descriptor)
            raise RuntimeError("another telegram-pi-bot process is already running") from None
        except OSError:
            os.close(descriptor)
            raise RuntimeError("the telegram-pi-bot process lock is unsafe or unavailable") from None
        self._descriptor = descriptor

    def release(self) -> None:
        if self._descriptor < 0:
            return
        try:
            os.ftruncate(self._descriptor, 0)
            os.fsync(self._descriptor)
        except OSError:
            pass
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        self._descriptor = -1


def _opaque_key(value: str) -> str:
    import hashlib

    return hashlib.blake2s(value.encode("utf-8"), digest_size=10).hexdigest()
