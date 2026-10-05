import gzip
import io
import json
import os
import socket
import tarfile
import tempfile
import unittest
import zipfile
import zlib
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from unittest import mock

import telegram_pi_bot.artifact_ipc as artifact_ipc
from telegram_pi_bot.artifact_ipc import (
    ArtifactRejected,
    sensitive_path_reason,
    validate_and_stage,
)
from telegram_pi_bot.model import ArtifactRequest


@dataclass(frozen=True)
class TestPolicy:
    allowed_root: Path
    state_dir: Path
    staging_dir: Path
    outbound_artifacts_per_turn: int = 5
    outbound_total_bytes: int = 50 * 1024 * 1024
    outbound_file_bytes: int = 20 * 1024 * 1024
    outbound_image_bytes: int = 10 * 1024 * 1024
    staging_retention_seconds: int = 24 * 60 * 60
    metadata_retention_seconds: int = 30 * 24 * 60 * 60
    ipc_timeout_seconds: float = 0.1
    ipc_max_frame_bytes: int = 64 * 1024


def test_policy(temp_root: Path) -> TestPolicy:
    state = temp_root / "telegram-pi-bot-state"
    return TestPolicy(
        allowed_root=temp_root,
        state_dir=state,
        staging_dir=state / "artifacts",
    )


def binary_fixture(kind: str, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    extensions = {
        "pdf": ".pdf",
        "text": ".txt",
        "json": ".json",
        "jsonl": ".jsonl",
        "yaml": ".yaml",
        "xml": ".xml",
        "docx": ".docx",
        "zip": ".zip",
        "tar": ".tar",
        "gzip": ".gz",
        "png": ".png",
        "jpeg": ".jpg",
        "webp": ".webp",
        "unknown": ".bin",
    }
    path = root / f"fixture{extensions[kind]}"
    if kind == "pdf":
        data = b"%PDF-1.7\nfixture report\n%%EOF\n"
    elif kind in {"text", "json", "jsonl", "yaml", "xml"}:
        data = {
            "text": b"Quarterly report\n",
            "json": b'{"ok":true}\n',
            "jsonl": b'{"row":1}\n',
            "yaml": b"title: report\n",
            "xml": b"<?xml version=\"1.0\"?><report/>\n",
        }[kind]
    elif kind == "docx":
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
        data = output.getvalue()
    elif kind == "zip":
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.txt", "report")
        data = output.getvalue()
    elif kind == "tar":
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            payload = b"report"
            info = tarfile.TarInfo("report.txt")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        data = output.getvalue()
    elif kind == "gzip":
        data = gzip.compress(b"report")
    elif kind == "png":
        ihdr = (13).to_bytes(4, "big") + b"IHDR" + (1).to_bytes(4, "big") * 2
        ihdr += bytes((8, 2, 0, 0, 0))
        ihdr += zlib.crc32(ihdr[4:]).to_bytes(4, "big")
        data = b"\x89PNG\r\n\x1a\n" + ihdr + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    elif kind == "jpeg":
        data = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    elif kind == "webp":
        payload = b"VP8 " + b"\x00\x00\x00"
        data = b"RIFF" + (4 + len(payload)).to_bytes(4, "little") + b"WEBP" + payload
    else:
        data = b"\x00\x01\x02\x03unknown binary"
    path.write_bytes(data)
    return path


def validate_rejection(path: Path | None, policy: TestPolicy, *, kind: str = "file") -> str | None:
    if path is None:
        return None
    try:
        result = validate_and_stage(ArtifactRequest(str(path), kind), policy)
    except Exception as error:
        reason = getattr(error, "reason", None)
        if reason is None:
            raise
        return str(getattr(reason, "value", reason))
    reason = getattr(result, "reason", None)
    return str(getattr(reason, "value", reason)) if reason is not None else None


class ArtifactValidationTests(unittest.TestCase):
    def test_hidden_and_secret_like_component_matrix(self):
        cases = {
            ".git-credentials": "hidden_path_component",
            ".wrangler/x.toml": "hidden_path_component",
            "comfyui-backup/credentials/huggingface_token": "secret_path_component",
            "Desktop/._mcp_token.txt": "hidden_path_component",
            ".vscode-server/.abc.token": "hidden_path_component",
            ".local/share/fish/fish_history": "hidden_path_component",
            ".antigravity-server/config": "hidden_path_component",
            ".antigravity-ide-server/config": "hidden_path_component",
            ".windsurf-server/config": "hidden_path_component",
            ".bash_history": "hidden_path_component",
            ".steam/steam.token": "hidden_path_component",
            ".github/workflows/test.yml": "hidden_path_component",
            "reports/customer-passwords.txt": "secret_path_component",
            "reports/id_rsa.pub": "secret_path_component",
            "reports/access.pem": "secret_path_component",
        }
        for relative, expected in cases.items():
            with self.subTest(relative=relative):
                self.assertEqual(sensitive_path_reason(PurePosixPath(relative)), expected)

    def test_tokenizer_basename_exception_is_narrow(self):
        self.assertIsNone(sensitive_path_reason(PurePosixPath("models/tokenizer.json")))
        self.assertEqual(
            sensitive_path_reason(PurePosixPath("models/tokenizer_access_token.json")),
            "secret_path_component",
        )
        self.assertEqual(
            sensitive_path_reason(PurePosixPath("models/tokenizer-token.txt")),
            "secret_path_component",
        )
        self.assertEqual(
            sensitive_path_reason(PurePosixPath("models/tokenizer_secret.json")),
            "secret_path_component",
        )
        self.assertEqual(
            sensitive_path_reason(PurePosixPath("tokenizer-cache/model.json")),
            "secret_path_component",
        )
        self.assertEqual(
            sensitive_path_reason(PurePosixPath("models/tokenizer.api_key.json")),
            "secret_path_component",
        )

    def test_visible_text_report_is_allowed(self):
        self.assertIsNone(sensitive_path_reason(PurePosixPath("reports/quarterly.txt")))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            policy = test_policy(root)
            path = binary_fixture("text", root / "reports")
            self.assertIsNone(validate_rejection(path, policy))

    def test_supported_documents_archives_and_image_signatures(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for kind in ("pdf", "text", "json", "jsonl", "yaml", "xml", "docx", "zip", "tar", "gzip"):
                with self.subTest(kind=kind):
                    path = binary_fixture(kind, root / kind)
                    self.assertIsNone(validate_rejection(path, test_policy(root)))
            for kind in ("png", "jpeg", "webp"):
                with self.subTest(kind=kind):
                    path = binary_fixture(kind, root / kind)
                    self.assertIsNone(
                        validate_rejection(path, test_policy(root), kind="image")
                    )

    def test_rejects_unknown_and_extension_signature_mismatch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            unknown = binary_fixture("unknown", root / "unknown")
            self.assertEqual(validate_rejection(unknown, test_policy(root)), "unsupported_type")
            mismatch = root / "mismatch.jpg"
            mismatch.write_bytes(b"%PDF-1.7\nnot a jpeg")
            self.assertEqual(validate_rejection(mismatch, test_policy(root), kind="image"), "signature_extension_mismatch")

    def test_rejects_zero_length_outside_nonregular_and_special_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            policy = test_policy(root)
            zero = root / "zero.txt"
            zero.touch()
            self.assertEqual(validate_rejection(zero, policy), "empty_file")
            self.assertEqual(validate_rejection(Path("/etc/passwd"), policy), "outside_allowed_root")
            self.assertEqual(validate_rejection(root, policy), "not_regular_file")

            fifo = root / "pipe.pdf"
            os.mkfifo(fifo)
            self.assertEqual(validate_rejection(fifo, policy), "not_regular_file")

            socket_path = root / "socket.pdf"
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(str(socket_path))
                self.assertEqual(validate_rejection(socket_path, policy), "not_regular_file")
            finally:
                listener.close()

    def test_resolved_symlinks_are_checked_before_target_bytes_are_staged(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            secret_dir = root / ".private"
            secret_dir.mkdir()
            secret = secret_dir / "credentials.txt"
            secret.write_bytes(b"must not be copied")
            alias = root / "report.txt"
            alias.symlink_to(secret)
            policy = test_policy(root)
            self.assertEqual(validate_rejection(alias, policy), "hidden_path_component")
            self.assertFalse(policy.staging_dir.exists())

    def test_copied_bytes_are_revalidated_after_signature_inspection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            policy = test_policy(root)
            source = root / "report.pdf"
            original = b"%PDF-1.7\nsafe report\n%%EOF\n"
            replacement = b"\x7fELF" + b"X" * (len(original) - 4)
            source.write_bytes(original)
            real_read = os.read
            mutated = False

            def mutate_after_prefix_read(descriptor, size):
                nonlocal mutated
                if not mutated:
                    mutated = True
                    with source.open("r+b") as handle:
                        handle.write(replacement)
                        handle.flush()
                        os.fsync(handle.fileno())
                return real_read(descriptor, size)

            with mock.patch.object(artifact_ipc.os, "read", side_effect=mutate_after_prefix_read):
                with self.assertRaises(ArtifactRejected):
                    validate_and_stage(ArtifactRequest(str(source), "file"), policy)


if __name__ == "__main__":
    unittest.main()
