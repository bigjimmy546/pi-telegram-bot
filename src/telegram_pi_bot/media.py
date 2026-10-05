"""Bounded inbound media staging and Groq voice transcription."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import os
import re
import stat
import time
import uuid
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


COPY_CHUNK_BYTES = 64 * 1024
GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"

_TEXT_EXTENSIONS = {
    ".c",
    ".cc",
    ".conf",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".log",
    ".md",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".rst",
    ".sh",
    ".sql",
    ".svg",
    ".tex",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_OOXML_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
_ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tgz"}
_DOCUMENT_EXTENSIONS = {".pdf", *_TEXT_EXTENSIONS, *_OOXML_EXTENSIONS, *_ARCHIVE_EXTENSIONS}
_VOICE_EXTENSIONS = {".aac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".webm"}
_GROQ_NATIVE_EXTENSIONS = _VOICE_EXTENSIONS - {".aac"}
_OWNED_NAME = re.compile(r"[0-9a-f]{32}(?:\.[a-z0-9]{1,12})?(?:\.part)?")


class MediaError(ValueError):
    """A bounded media failure safe to show to the authorized user."""


@dataclass(frozen=True, slots=True)
class AttachmentPolicy:
    root: Path
    max_items: int = 10
    max_total_bytes: int = 50 * 1024 * 1024
    voice_bytes: int = 20 * 1024 * 1024
    document_bytes: int = 20 * 1024 * 1024
    photo_bytes: int = 10 * 1024 * 1024
    retention_seconds: int = 24 * 60 * 60

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ValueError("attachment root must be absolute")
        limits = (
            self.max_items,
            self.max_total_bytes,
            self.voice_bytes,
            self.document_bytes,
            self.photo_bytes,
            self.retention_seconds,
        )
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("attachment limits must be positive integers")

    @classmethod
    def from_config(cls, config: Any) -> AttachmentPolicy:
        return cls(
            root=config.state_dir / "attachments",
            max_items=config.inbound_items,
            max_total_bytes=config.inbound_bundle_bytes,
            voice_bytes=config.voice_bytes,
            document_bytes=config.document_bytes,
            photo_bytes=config.photo_bytes,
            retention_seconds=config.staging_retention_seconds,
        )


@dataclass(frozen=True, slots=True)
class Attachment:
    attachment_id: str
    telegram_file_id: str
    kind: str
    display_name: str
    path: Path
    size_bytes: int
    sha256: str
    mime_type: str
    created_at_ms: int


class AttachmentStore:
    def __init__(self, policy: AttachmentPolicy) -> None:
        self.policy = policy
        policy.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(policy.root, 0o700)

    async def stage(
        self,
        *,
        kind: str,
        telegram_file_id: str,
        filename: str | None,
        mime_type: str | None,
        expected_size: int | None,
        chunks: AsyncIterable[bytes],
        existing: Iterable[Attachment] = (),
        now_ms: int | None = None,
    ) -> Attachment:
        if kind not in {"voice", "photo", "document"}:
            raise MediaError("unsupported attachment kind")
        if not isinstance(telegram_file_id, str) or not telegram_file_id:
            raise MediaError("attachment identity is invalid")
        display_name = sanitize_filename(filename, kind)
        mime = mime_type.strip().lower() if isinstance(mime_type, str) else ""
        _validate_metadata(kind, display_name, mime)
        records = tuple(existing)
        if len(records) >= self.policy.max_items:
            raise MediaError("attachment item limit exceeded")
        current_total = sum(record.size_bytes for record in records)
        item_limit = self._item_limit(kind)
        if expected_size is not None:
            if type(expected_size) is not int or expected_size < 0:
                raise MediaError("attachment size metadata is invalid")
            if expected_size > item_limit:
                raise MediaError("attachment exceeds its size limit")
            if current_total + expected_size > self.policy.max_total_bytes:
                raise MediaError("attachment bundle size limit exceeded")

        attachment_id = uuid.uuid4().hex
        suffix = _safe_suffix(display_name)
        final_path = self.policy.root / f"{attachment_id}{suffix}"
        part_path = self.policy.root / f"{attachment_id}{suffix}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(part_path, flags, 0o600)
        except OSError as error:
            raise MediaError("attachment staging is unavailable") from error

        size = 0
        digest = hashlib.sha256()
        prefix = bytearray()
        decoder = (
            codecs.getincrementaldecoder("utf-8")("strict")
            if kind == "document" and Path(display_name).suffix.lower() in _TEXT_EXTENSIONS
            else None
        )
        completed = False
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes) or not chunk:
                    raise MediaError("attachment download was invalid")
                size += len(chunk)
                if size > item_limit:
                    raise MediaError("attachment exceeds its size limit")
                if current_total + size > self.policy.max_total_bytes:
                    raise MediaError("attachment bundle size limit exceeded")
                digest.update(chunk)
                if len(prefix) < 512:
                    prefix.extend(chunk[: 512 - len(prefix)])
                if decoder is not None:
                    try:
                        decoder.decode(chunk)
                    except UnicodeDecodeError as error:
                        raise MediaError("attachment type did not match its name") from error
                _write_all(descriptor, chunk)
            if expected_size is not None and size != expected_size:
                raise MediaError("attachment size did not match Telegram metadata")
            if size == 0:
                raise MediaError("attachment download was empty")
            if decoder is not None:
                try:
                    decoder.decode(b"", final=True)
                except UnicodeDecodeError as error:
                    raise MediaError("attachment type did not match its name") from error
            _validate_signature(kind, display_name, bytes(prefix))
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(part_path, final_path)
            os.chmod(final_path, 0o600)
            completed = True
        except OSError as error:
            raise MediaError("attachment could not be stored") from error
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if not completed:
                try:
                    part_path.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    final_path.unlink(missing_ok=True)
                except OSError:
                    pass

        return Attachment(
            attachment_id=attachment_id,
            telegram_file_id=telegram_file_id,
            kind=kind,
            display_name=display_name,
            path=final_path,
            size_bytes=size,
            sha256=digest.hexdigest(),
            mime_type=mime,
            created_at_ms=int(time.time() * 1_000) if now_ms is None else now_ms,
        )

    def release(self, attachment: Attachment) -> None:
        path = attachment.path.absolute()
        if path.parent != self.policy.root.absolute() or not _OWNED_NAME.fullmatch(path.name):
            raise MediaError("attachment ownership is invalid")
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise MediaError("attachment could not be released") from error

    def read_photo(self, path: Path) -> tuple[str, bytes]:
        absolute = path.absolute()
        if (
            absolute.parent != self.policy.root.absolute()
            or not _OWNED_NAME.fullmatch(absolute.name)
            or absolute.suffix.lower() not in {".jpeg", ".jpg", ".png", ".webp"}
        ):
            raise MediaError("photo attachment ownership is invalid")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = os.open(absolute, flags)
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or not 0 < details.st_size <= self.policy.photo_bytes
            ):
                raise MediaError("photo attachment is invalid")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                content = handle.read(self.policy.photo_bytes + 1)
        except OSError:
            raise MediaError("photo attachment is unavailable") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(content) != details.st_size:
            raise MediaError("photo attachment changed while reading")
        _validate_signature("photo", absolute.name, content[:512])
        mime = {
            ".jpeg": "image/jpeg",
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }[absolute.suffix.lower()]
        return mime, content

    def sweep(self, now_ms: int, *, protected: Iterable[Path] = ()) -> int:
        cutoff_ns = (now_ms - self.policy.retention_seconds * 1_000) * 1_000_000
        protected_paths = {path.absolute() for path in protected}
        removed = 0
        for child in tuple(self.policy.root.iterdir()):
            if (
                child.absolute() in protected_paths
                or not _OWNED_NAME.fullmatch(child.name)
            ):
                continue
            try:
                details = child.stat(follow_symlinks=False)
                if stat.S_ISREG(details.st_mode) and details.st_mtime_ns <= cutoff_ns:
                    child.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
        return removed

    def _item_limit(self, kind: str) -> int:
        return {
            "voice": self.policy.voice_bytes,
            "photo": self.policy.photo_bytes,
            "document": self.policy.document_bytes,
        }[kind]


class GroqTranscriber:
    def __init__(
        self,
        key_file: Path,
        *,
        client: httpx.AsyncClient | None = None,
        create_subprocess=asyncio.create_subprocess_exec,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._key_file = key_file
        self._client = client
        self._create_subprocess = create_subprocess
        self._timeout_seconds = timeout_seconds

    async def transcribe(self, source: Path) -> str:
        if not source.is_file():
            raise MediaError("voice attachment is unavailable")
        converted: Path | None = None
        try:
            audio = source
            if source.suffix.lower() not in _GROQ_NATIVE_EXTENSIONS:
                converted = source.with_name(f"{uuid.uuid4().hex}.wav")
                await self._convert(source, converted)
                audio = converted
            key = _read_private_key(self._key_file)
            if self._client is None:
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    return await self._post(client, audio, key)
            return await self._post(self._client, audio, key)
        finally:
            if converted is not None:
                try:
                    converted.unlink(missing_ok=True)
                except OSError:
                    pass

    async def _convert(self, source: Path, target: Path) -> None:
        try:
            process = await self._create_subprocess(
                "/usr/bin/ffmpeg",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-acodec",
                "pcm_s16le",
                str(target),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, _stderr = await process.communicate()
        except OSError:
            raise MediaError("voice conversion is unavailable") from None
        if process.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            raise MediaError("voice conversion failed")
        try:
            target.chmod(0o600)
        except OSError:
            raise MediaError("voice conversion failed") from None

    async def _post(self, client: httpx.AsyncClient, audio: Path, key: str) -> str:
        try:
            with audio.open("rb") as handle:
                response = await client.post(
                    GROQ_TRANSCRIPTION_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    data={"model": GROQ_MODEL},
                    files={"file": (audio.name, handle, "application/octet-stream")},
                    timeout=self._timeout_seconds,
                )
        except httpx.TimeoutException:
            raise MediaError("voice transcription timed out") from None
        except (httpx.HTTPError, OSError):
            raise MediaError("voice transcription failed") from None
        if not 200 <= response.status_code < 300:
            raise MediaError("voice transcription service returned an error")
        try:
            payload = response.json()
        except ValueError:
            raise MediaError("voice transcription response was invalid") from None
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise MediaError("voice transcription returned no text")
        return text.strip()


def supported_document_metadata(filename: object, mime_type: object) -> bool:
    if not isinstance(filename, str) or not filename or "\0" in filename:
        return False
    suffix = Path(filename).suffix.lower()
    mime = mime_type.strip().lower() if isinstance(mime_type, str) else ""
    return suffix in _DOCUMENT_EXTENSIONS and (
        mime.startswith("text/")
        or suffix in _TEXT_EXTENSIONS
        or mime in {
            "application/gzip",
            "application/json",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/x-gzip",
            "application/x-tar",
            "application/xml",
            "application/zip",
            "application/octet-stream",
            "text/xml",
        }
        or not mime
    )


def sanitize_filename(filename: str | None, kind: str) -> str:
    fallback = {"voice": "voice.ogg", "photo": "photo.jpg", "document": "document.bin"}[kind]
    if not isinstance(filename, str) or not filename.strip():
        return fallback
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", basename).strip("._")
    if not value:
        return fallback
    return value[:120]


def _validate_metadata(kind: str, filename: str, mime: str) -> None:
    suffix = Path(filename).suffix.lower()
    if kind == "voice" and suffix not in _VOICE_EXTENSIONS:
        raise MediaError("unsupported voice type")
    if kind == "photo" and suffix not in {".jpeg", ".jpg", ".png", ".webp"}:
        raise MediaError("unsupported photo type")
    if kind == "document" and not supported_document_metadata(filename, mime):
        raise MediaError("unsupported document type")


def _validate_signature(kind: str, filename: str, prefix: bytes) -> None:
    suffix = Path(filename).suffix.lower()
    valid = False
    if kind == "photo":
        valid = (
            suffix in {".jpg", ".jpeg"} and prefix.startswith(b"\xff\xd8\xff")
            or suffix == ".png" and prefix.startswith(b"\x89PNG\r\n\x1a\n")
            or suffix == ".webp" and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"
        )
    elif kind == "voice":
        valid = (
            suffix in {".ogg", ".oga", ".opus"} and prefix.startswith(b"OggS")
            or suffix == ".mp3" and (prefix.startswith(b"ID3") or prefix[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"})
            or suffix == ".m4a" and prefix[4:8] == b"ftyp"
            or suffix == ".aac" and len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xF6 == 0xF0
            or suffix == ".wav" and prefix[:4] == b"RIFF" and prefix[8:12] == b"WAVE"
            or suffix == ".webm" and prefix.startswith(b"\x1aE\xdf\xa3")
        )
    elif suffix in _TEXT_EXTENSIONS:
        valid = b"\0" not in prefix
    elif suffix == ".pdf":
        valid = prefix.startswith(b"%PDF-")
    elif suffix in {".zip", *_OOXML_EXTENSIONS}:
        valid = prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    elif suffix in {".gz", ".tgz"}:
        valid = prefix.startswith(b"\x1f\x8b")
    elif suffix == ".tar":
        valid = len(prefix) >= 262 and prefix[257:262] == b"ustar"
    if not valid:
        raise MediaError("attachment type did not match its name")


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix) else ""


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short attachment write")
        view = view[written:]


def _read_private_key(path: Path) -> str:
    descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise MediaError("Groq voice credential is unavailable")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = handle.read(4097).strip()
    except (OSError, UnicodeError):
        raise MediaError("Groq voice credential is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not value or len(value) > 4096:
        raise MediaError("Groq voice credential is unavailable")
    return value
