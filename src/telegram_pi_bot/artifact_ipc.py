"""Private artifact validation, durable staging, and Pi-extension IPC."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import struct
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from telegram_pi_bot.model import ArtifactReceipt, ArtifactRequest


DEFAULT_IPC_TIMEOUT_SECONDS = 30.0
DEFAULT_IPC_FRAME_BYTES = 16 * 1024
MAX_CAPTION_CHARS = 1024
MAX_PATH_BYTES = 4096
COPY_CHUNK_BYTES = 64 * 1024


class ArtifactPolicyLike(Protocol):
    allowed_root: Path
    state_dir: Path
    staging_dir: Path
    outbound_artifacts_per_turn: int
    outbound_total_bytes: int
    outbound_file_bytes: int
    outbound_image_bytes: int
    staging_retention_seconds: int
    metadata_retention_seconds: int


@dataclass(frozen=True, slots=True)
class ArtifactPolicy:
    allowed_root: Path
    state_dir: Path
    staging_dir: Path
    outbound_artifacts_per_turn: int = 5
    outbound_total_bytes: int = 50 * 1024 * 1024
    outbound_file_bytes: int = 20 * 1024 * 1024
    outbound_image_bytes: int = 10 * 1024 * 1024
    staging_retention_seconds: int = 24 * 60 * 60
    metadata_retention_seconds: int = 30 * 24 * 60 * 60
    ipc_timeout_seconds: float = DEFAULT_IPC_TIMEOUT_SECONDS
    ipc_max_frame_bytes: int = DEFAULT_IPC_FRAME_BYTES

    @classmethod
    def from_config(cls, config: Any) -> ArtifactPolicy:
        return cls(
            allowed_root=config.cwd,
            state_dir=config.state_dir,
            staging_dir=config.state_dir / "artifacts" / "staging",
            outbound_artifacts_per_turn=config.outbound_artifacts_per_turn,
            outbound_total_bytes=config.outbound_total_bytes,
            outbound_file_bytes=config.outbound_file_bytes,
            outbound_image_bytes=config.outbound_image_bytes,
            staging_retention_seconds=config.staging_retention_seconds,
            metadata_retention_seconds=config.metadata_retention_seconds,
        )


class ArtifactRejectReason(StrEnum):
    INVALID_REQUEST = "invalid_request"
    OUTSIDE_ALLOWED_ROOT = "outside_allowed_root"
    HIDDEN_PATH_COMPONENT = "hidden_path_component"
    SECRET_PATH_COMPONENT = "secret_path_component"
    NOT_REGULAR_FILE = "not_regular_file"
    EMPTY_FILE = "empty_file"
    UNSUPPORTED_TYPE = "unsupported_type"
    SIGNATURE_EXTENSION_MISMATCH = "signature_extension_mismatch"
    ITEM_SIZE_EXCEEDED = "item_size_exceeded"
    AGGREGATE_SIZE_EXCEEDED = "aggregate_size_exceeded"
    ARTIFACT_COUNT_EXCEEDED = "artifact_count_exceeded"
    SOURCE_CHANGED = "source_changed"
    STAGING_FAILED = "staging_failed"
    AUTHENTICATION_FAILED = "authentication_failed"
    CHILD_MISMATCH = "child_mismatch"


class ArtifactRejected(ValueError):
    def __init__(self, reason: ArtifactRejectReason | str) -> None:
        self.reason = ArtifactRejectReason(reason)
        super().__init__(self.reason.value)


_SENSITIVE = re.compile(
    r"secret|credential|token|passw|api[_-]?key|\.pem$|\.key$|^id_(?:rsa|ed25519|ecdsa)",
    re.IGNORECASE,
)
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
    ".json",
    ".jsonl",
}
_OOXML_EXTENSIONS = {".docx", ".xlsx", ".pptx"}


def sensitive_path_reason(relative_path: PurePosixPath) -> str | None:
    if relative_path.is_absolute():
        return ArtifactRejectReason.OUTSIDE_ALLOWED_ROOT.value
    parts = relative_path.parts
    for index, part in enumerate(parts):
        if part.startswith("."):
            return ArtifactRejectReason.HIDDEN_PATH_COMPONENT.value
        for match in _SENSITIVE.finditer(part):
            tokenizer_exception = (
                index == len(parts) - 1
                and part.lower().startswith("tokenizer")
                and match.start() == 0
                and match.group(0).lower() == "token"
            )
            if not tokenizer_exception:
                return ArtifactRejectReason.SECRET_PATH_COMPONENT.value
    return None


def validate_and_stage(
    request: ArtifactRequest,
    policy: ArtifactPolicyLike,
    existing: Sequence[ArtifactReceipt] = (),
) -> ArtifactReceipt:
    _validate_request(request)
    if len(existing) >= policy.outbound_artifacts_per_turn:
        raise ArtifactRejected(ArtifactRejectReason.ARTIFACT_COUNT_EXCEEDED)
    current_total = sum(item.size_bytes for item in existing)
    allowed_root = policy.allowed_root.resolve(strict=True)
    requested = Path(request.path)
    candidate = requested if requested.is_absolute() else allowed_root / requested
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ArtifactRejected(ArtifactRejectReason.NOT_REGULAR_FILE) from error
    _validate_source_path(resolved, allowed_root)
    try:
        preliminary = resolved.stat()
    except OSError as error:
        raise ArtifactRejected(ArtifactRejectReason.NOT_REGULAR_FILE) from error
    if not stat.S_ISREG(preliminary.st_mode):
        raise ArtifactRejected(ArtifactRejectReason.NOT_REGULAR_FILE)

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(resolved, flags)
    except OSError as error:
        raise ArtifactRejected(ArtifactRejectReason.NOT_REGULAR_FILE) from error
    try:
        metadata = os.fstat(source_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ArtifactRejected(ArtifactRejectReason.NOT_REGULAR_FILE)
        if metadata.st_size == 0:
            raise ArtifactRejected(ArtifactRejectReason.EMPTY_FILE)
        actual = _opened_path(source_fd)
        _validate_source_path(actual, allowed_root)
        item_limit = (
            policy.outbound_image_bytes
            if request.kind == "image"
            else policy.outbound_file_bytes
        )
        if metadata.st_size > item_limit:
            raise ArtifactRejected(ArtifactRejectReason.ITEM_SIZE_EXCEEDED)
        if current_total + metadata.st_size > policy.outbound_total_bytes:
            raise ArtifactRejected(ArtifactRejectReason.AGGREGATE_SIZE_EXCEEDED)
        prefix = os.pread(source_fd, 512, 0)
        is_text = _validate_file_type(actual, request.kind, prefix)
        return _copy_and_persist(
            source_fd,
            actual,
            request,
            policy,
            existing_total=current_total,
            item_limit=item_limit,
            validate_utf8=is_text,
        )
    finally:
        os.close(source_fd)


def verify_staged(receipt: ArtifactReceipt) -> None:
    path = receipt.staged_path
    if path is None:
        raise ArtifactRejected(ArtifactRejectReason.STAGING_FAILED)
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArtifactRejected(ArtifactRejectReason.STAGING_FAILED) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != receipt.size_bytes:
            raise ArtifactRejected(ArtifactRejectReason.SOURCE_CHANGED)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, COPY_CHUNK_BYTES):
            digest.update(chunk)
        if not secrets.compare_digest(digest.hexdigest(), receipt.sha256):
            raise ArtifactRejected(ArtifactRejectReason.SOURCE_CHANGED)
    finally:
        os.close(descriptor)


def delete_staged(receipt: ArtifactReceipt, policy: ArtifactPolicyLike) -> None:
    if receipt.staged_path is None:
        return
    staging_dir = policy.staging_dir.absolute()
    path = receipt.staged_path.absolute()
    if path.parent != staging_dir or not _uuid_stem(path.name):
        raise ValueError("staged artifact path is outside the managed directory")
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def sweep_artifacts(
    policy: ArtifactPolicyLike, *, now_seconds: float | None = None
) -> tuple[int, int]:
    now = time.time() if now_seconds is None else now_seconds
    staged = _sweep_directory(
        policy.staging_dir,
        now - policy.staging_retention_seconds,
        metadata=False,
    )
    metadata = _sweep_directory(
        _metadata_dir(policy),
        now - policy.metadata_retention_seconds,
        metadata=True,
    )
    return staged, metadata


class ArtifactBroker:
    def __init__(self, policy: ArtifactPolicyLike, turn_id: str) -> None:
        self._policy = policy
        self._turn_id = turn_id
        self._server: asyncio.AbstractServer | None = None
        self._receipts: list[ArtifactReceipt] = []
        self._responses: dict[str, dict[str, Any]] = {}
        self._child_pid: int | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._request_lock = asyncio.Lock()
        self._closed = False
        self.socket_path = Path()
        self.capability = secrets.token_hex(32)

    @classmethod
    async def start(
        cls, policy: ArtifactPolicyLike, turn_id: str
    ) -> ArtifactBroker:
        if not turn_id or len(turn_id) > 128 or any(ord(char) < 32 for char in turn_id):
            raise ValueError("turn ID is invalid")
        broker = cls(policy, turn_id)
        socket_dir = policy.state_dir / "ipc"
        _ensure_private_dir(socket_dir)
        broker.socket_path = socket_dir / f"a-{uuid.uuid4().hex}.sock"
        if len(os.fsencode(broker.socket_path)) >= 104:
            raise ValueError("artifact socket path is too long")

        def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.create_task(
                broker._handle_client(reader, writer),
                name="artifact-ipc-client",
            )
            broker._tasks.add(task)
            task.add_done_callback(broker._client_done)

        frame_limit = _positive_int(
            getattr(policy, "ipc_max_frame_bytes", DEFAULT_IPC_FRAME_BYTES),
            "IPC frame limit",
        )
        try:
            broker._server = await asyncio.start_unix_server(
                connected,
                path=broker.socket_path,
                limit=frame_limit + 1,
            )
            os.chmod(broker.socket_path, 0o600)
        except BaseException:
            if broker._server is not None:
                broker._server.close()
                await broker._server.wait_closed()
            try:
                broker.socket_path.unlink()
            except FileNotFoundError:
                pass
            raise
        return broker

    def child_environment(self) -> Mapping[str, str]:
        return {
            "TELEGRAM_PI_ARTIFACT_SOCKET": str(self.socket_path),
            "TELEGRAM_PI_ARTIFACT_CAPABILITY": self.capability,
        }

    def bind_child(self, pid: int) -> None:
        if type(pid) is not int or pid <= 0:
            raise ValueError("child PID must be positive")
        if self._child_pid is not None:
            raise ValueError("artifact broker child is already bound")
        self._child_pid = pid

    def receipts(self) -> tuple[ArtifactReceipt, ...]:
        return tuple(self._receipts)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for writer in tuple(self._writers):
            writer.close()
        if self._writers:
            await asyncio.gather(
                *(writer.wait_closed() for writer in tuple(self._writers)),
                return_exceptions=True,
            )
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(writer)
        try:
            timeout = _positive_number(
                getattr(
                    self._policy,
                    "ipc_timeout_seconds",
                    DEFAULT_IPC_TIMEOUT_SECONDS,
                ),
                "IPC timeout",
            )
            frame_limit = _positive_int(
                getattr(
                    self._policy,
                    "ipc_max_frame_bytes",
                    DEFAULT_IPC_FRAME_BYTES,
                ),
                "IPC frame limit",
            )
            try:
                async with asyncio.timeout(timeout):
                    line = await reader.readuntil(b"\n")
            except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                return
            if len(line) > frame_limit or not line.endswith(b"\n"):
                return
            try:
                raw = json.loads(
                    line.decode("utf-8", errors="strict"),
                    parse_constant=_reject_json_constant,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                await self._send(writer, _rejection("", ArtifactRejectReason.INVALID_REQUEST))
                return
            async with self._request_lock:
                response = self._process_request(raw, writer)
            await self._send(writer, response)
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _client_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _process_request(
        self, raw: Any, writer: asyncio.StreamWriter
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return _rejection("", ArtifactRejectReason.INVALID_REQUEST)
        request_id = raw.get("id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            return _rejection("", ArtifactRejectReason.INVALID_REQUEST)
        capability = raw.get("capability")
        if not isinstance(capability, str) or not secrets.compare_digest(
            capability, self.capability
        ):
            return _rejection(request_id, ArtifactRejectReason.AUTHENTICATION_FAILED)
        peer = _peer_identity(writer)
        if peer is None or peer[1] != os.getuid():
            return _rejection(request_id, ArtifactRejectReason.AUTHENTICATION_FAILED)
        peer_pid = peer[0]
        if self._child_pid is None or self._child_pid != peer_pid:
            return _rejection(request_id, ArtifactRejectReason.CHILD_MISMATCH)
        prior = self._responses.get(request_id)
        if prior is not None:
            return prior
        kind = raw.get("kind")
        path = raw.get("path")
        caption = raw.get("caption", "")
        if (
            kind not in {"file", "image"}
            or not isinstance(path, str)
            or not isinstance(caption, str)
        ):
            response = _rejection(request_id, ArtifactRejectReason.INVALID_REQUEST)
            self._responses[request_id] = response
            return response
        try:
            receipt = validate_and_stage(
                ArtifactRequest(path, kind, caption),
                self._policy,
                self._receipts,
            )
        except ArtifactRejected as error:
            response = _rejection(request_id, error.reason)
        else:
            self._receipts.append(receipt)
            response = {
                "id": request_id,
                "accepted": True,
                "message": "Artifact accepted for authorized Telegram delivery.",
            }
        self._responses[request_id] = response
        return response

    async def _send(self, writer: asyncio.StreamWriter, response: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        writer.write(encoded)
        await writer.drain()


def _validate_request(request: ArtifactRequest) -> None:
    if len(request.path.encode("utf-8")) > MAX_PATH_BYTES:
        raise ArtifactRejected(ArtifactRejectReason.INVALID_REQUEST)
    if len(request.caption) > MAX_CAPTION_CHARS or any(
        ord(character) < 32 and character not in "\n\t" for character in request.caption
    ):
        raise ArtifactRejected(ArtifactRejectReason.INVALID_REQUEST)


def _validate_source_path(path: Path, allowed_root: Path) -> PurePosixPath:
    try:
        relative = path.relative_to(allowed_root)
    except ValueError as error:
        raise ArtifactRejected(ArtifactRejectReason.OUTSIDE_ALLOWED_ROOT) from error
    reason = sensitive_path_reason(PurePosixPath(relative.as_posix()))
    if reason is not None:
        raise ArtifactRejected(reason)
    return PurePosixPath(relative.as_posix())


def _opened_path(descriptor: int) -> Path:
    try:
        raw = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError as error:
        raise ArtifactRejected(ArtifactRejectReason.SOURCE_CHANGED) from error
    if raw.endswith(" (deleted)"):
        raise ArtifactRejected(ArtifactRejectReason.SOURCE_CHANGED)
    try:
        return Path(raw).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ArtifactRejected(ArtifactRejectReason.SOURCE_CHANGED) from error


def _validate_file_type(path: Path, kind: str, prefix: bytes) -> bool:
    suffix = path.suffix.lower()
    if kind == "image":
        signatures = {
            ".png": prefix.startswith(b"\x89PNG\r\n\x1a\n"),
            ".jpg": prefix.startswith(b"\xff\xd8\xff"),
            ".jpeg": prefix.startswith(b"\xff\xd8\xff"),
            ".webp": len(prefix) >= 12
            and prefix.startswith(b"RIFF")
            and prefix[8:12] == b"WEBP",
        }
        if suffix not in signatures:
            raise ArtifactRejected(ArtifactRejectReason.UNSUPPORTED_TYPE)
        if not signatures[suffix]:
            raise ArtifactRejected(ArtifactRejectReason.SIGNATURE_EXTENSION_MISMATCH)
        return False
    if suffix == ".pdf":
        if not prefix.startswith(b"%PDF-"):
            raise ArtifactRejected(ArtifactRejectReason.SIGNATURE_EXTENSION_MISMATCH)
        return False
    if suffix in _TEXT_EXTENSIONS:
        if b"\x00" in prefix:
            raise ArtifactRejected(ArtifactRejectReason.SIGNATURE_EXTENSION_MISMATCH)
        return True
    if suffix in _OOXML_EXTENSIONS or suffix == ".zip":
        if not prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            raise ArtifactRejected(ArtifactRejectReason.SIGNATURE_EXTENSION_MISMATCH)
        return False
    if suffix == ".tar":
        if len(prefix) < 262 or prefix[257:262] != b"ustar":
            raise ArtifactRejected(ArtifactRejectReason.SIGNATURE_EXTENSION_MISMATCH)
        return False
    if suffix in {".gz", ".tgz"}:
        if not prefix.startswith(b"\x1f\x8b"):
            raise ArtifactRejected(ArtifactRejectReason.SIGNATURE_EXTENSION_MISMATCH)
        return False
    raise ArtifactRejected(ArtifactRejectReason.UNSUPPORTED_TYPE)


def _copy_and_persist(
    source_fd: int,
    source_path: Path,
    request: ArtifactRequest,
    policy: ArtifactPolicyLike,
    *,
    existing_total: int,
    item_limit: int,
    validate_utf8: bool,
) -> ArtifactReceipt:
    _ensure_private_dir(policy.staging_dir)
    metadata_dir = _metadata_dir(policy)
    _ensure_private_dir(metadata_dir)
    artifact_id = uuid.uuid4().hex
    suffix = source_path.suffix.lower()
    staged_path = policy.staging_dir / f"{artifact_id}{suffix}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        staged_fd = os.open(staged_path, flags, 0o600)
    except OSError as error:
        raise ArtifactRejected(ArtifactRejectReason.STAGING_FAILED) from error
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")() if validate_utf8 else None
    copied_prefix = bytearray()
    size = 0
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while chunk := os.read(source_fd, COPY_CHUNK_BYTES):
            if len(copied_prefix) < 512:
                copied_prefix.extend(chunk[: 512 - len(copied_prefix)])
            size += len(chunk)
            if size > item_limit:
                raise ArtifactRejected(ArtifactRejectReason.ITEM_SIZE_EXCEEDED)
            if existing_total + size > policy.outbound_total_bytes:
                raise ArtifactRejected(ArtifactRejectReason.AGGREGATE_SIZE_EXCEEDED)
            if decoder is not None:
                try:
                    decoder.decode(chunk, final=False)
                except UnicodeDecodeError as error:
                    raise ArtifactRejected(
                        ArtifactRejectReason.SIGNATURE_EXTENSION_MISMATCH
                    ) from error
            digest.update(chunk)
            _write_all(staged_fd, chunk)
        if size == 0:
            raise ArtifactRejected(ArtifactRejectReason.EMPTY_FILE)
        if decoder is not None:
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError as error:
                raise ArtifactRejected(
                    ArtifactRejectReason.SIGNATURE_EXTENSION_MISMATCH
                ) from error
        _validate_file_type(source_path, request.kind, bytes(copied_prefix))
        os.fsync(staged_fd)
    except BaseException:
        os.close(staged_fd)
        try:
            staged_path.unlink()
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(staged_fd)
    _fsync_directory(policy.staging_dir)

    receipt = ArtifactReceipt(
        artifact_id=artifact_id,
        filename=_safe_filename(source_path.name),
        kind=request.kind,
        size_bytes=size,
        sha256=digest.hexdigest(),
        staged_path=staged_path,
        caption=request.caption,
        created_at_ms=time.time_ns() // 1_000_000,
    )
    try:
        _persist_metadata(receipt, metadata_dir)
    except OSError as error:
        try:
            staged_path.unlink()
        except FileNotFoundError:
            pass
        raise ArtifactRejected(ArtifactRejectReason.STAGING_FAILED) from error
    return receipt


def _persist_metadata(receipt: ArtifactReceipt, metadata_dir: Path) -> None:
    target = metadata_dir / f"{receipt.artifact_id}.json"
    temporary = metadata_dir / f"{receipt.artifact_id}.tmp"
    payload = json.dumps(
        {
            "artifact_id": receipt.artifact_id,
            "filename": receipt.filename,
            "kind": receipt.kind,
            "size_bytes": receipt.size_bytes,
            "sha256": receipt.sha256,
            "created_at_ms": receipt.created_at_ms,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
        _fsync_directory(metadata_dir)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _metadata_dir(policy: ArtifactPolicyLike) -> Path:
    return policy.state_dir / "artifact-metadata"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if resolved != path.absolute() or not resolved.is_dir():
        raise ArtifactRejected(ArtifactRejectReason.STAGING_FAILED)
    os.chmod(resolved, 0o700)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short artifact write")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_filename(value: str) -> str:
    cleaned = "".join(
        "_" if ord(character) < 32 else character for character in Path(value).name
    ).strip()
    if not cleaned:
        return "artifact"
    return cleaned[:128]


def _sweep_directory(path: Path, cutoff: float, *, metadata: bool) -> int:
    try:
        entries = tuple(path.iterdir())
    except FileNotFoundError:
        return 0
    removed = 0
    for entry in entries:
        if metadata:
            managed = entry.suffix == ".json" and _uuid_stem(entry.name)
        else:
            managed = _uuid_stem(entry.name)
        if not managed:
            continue
        try:
            item_stat = entry.lstat()
            if not stat.S_ISREG(item_stat.st_mode) or item_stat.st_mtime > cutoff:
                continue
            entry.unlink()
            removed += 1
        except FileNotFoundError:
            continue
    return removed


def _uuid_stem(name: str) -> bool:
    stem = name.split(".", 1)[0]
    try:
        return uuid.UUID(stem).hex == stem
    except ValueError:
        return False


def _peer_identity(writer: asyncio.StreamWriter) -> tuple[int, int, int] | None:
    peer_socket = writer.get_extra_info("socket")
    if peer_socket is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        raw = peer_socket.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
    except OSError:
        return None
    return struct.unpack("3i", raw)


def _rejection(
    request_id: str, reason: ArtifactRejectReason
) -> dict[str, Any]:
    return {
        "id": request_id,
        "accepted": False,
        "reason": reason.value,
        "message": f"Artifact rejected: {reason.value}.",
    }


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _positive_number(value: Any, label: str) -> float:
    if type(value) not in {int, float} or value <= 0:
        raise ValueError(f"{label} must be positive")
    return float(value)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")
