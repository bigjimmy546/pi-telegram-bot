from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

import httpx

from telegram_pi_bot.media import (
    AttachmentPolicy,
    AttachmentStore,
    GroqTranscriber,
    MediaError,
)


async def _chunks(*values: bytes):
    for value in values:
        yield value


def _policy(root: Path, **changes) -> AttachmentPolicy:
    values = {
        "root": root,
        "max_items": 10,
        "max_total_bytes": 50 * 1024 * 1024,
        "voice_bytes": 20 * 1024 * 1024,
        "document_bytes": 20 * 1024 * 1024,
        "photo_bytes": 10 * 1024 * 1024,
        "retention_seconds": 86400,
    }
    values.update(changes)
    return AttachmentPolicy(**values)


class FakeResponse:
    status_code = 200

    def json(self):
        return {"text": "transcribed request"}


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = []
        self.error = error

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        kwargs["files"]["file"][1].read()
        return FakeResponse()


class AttachmentStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_staged_photo_is_reopened_without_following_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AttachmentStore(_policy(Path(directory) / "attachments"))
            payload = b"\x89PNG\r\n\x1a\nphoto"
            attachment = await store.stage(
                kind="photo",
                telegram_file_id="photo-1",
                filename="photo.png",
                mime_type="image/png",
                expected_size=len(payload),
                chunks=_chunks(payload),
            )

            self.assertEqual(store.read_photo(attachment.path), ("image/png", payload))
            attachment.path.unlink()
            attachment.path.symlink_to(Path(directory) / "outside.png")
            with self.assertRaisesRegex(MediaError, "unavailable"):
                store.read_photo(attachment.path)

    async def test_random_private_staging_sanitizes_name_and_preserves_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AttachmentStore(_policy(Path(directory) / "attachments"))
            content = b"hello\n"
            attachment = await store.stage(
                kind="document",
                telegram_file_id="file-1",
                filename="../../hostile report.txt",
                mime_type="text/plain",
                expected_size=len(content),
                chunks=_chunks(content),
            )

            self.assertEqual(attachment.display_name, "hostile_report.txt")
            self.assertNotIn("hostile", attachment.path.name)
            self.assertEqual(stat.S_IMODE(attachment.path.stat().st_mode), 0o600)
            self.assertEqual(attachment.size_bytes, len(content))
            self.assertEqual(attachment.sha256, hashlib.sha256(content).hexdigest())

    async def test_stream_stops_one_byte_over_item_cap_and_cleans_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cases = (
                ("document", "report.txt", "text/plain", "document_bytes"),
                ("photo", "photo.png", "image/png", "photo_bytes"),
                ("voice", "voice.ogg", "audio/ogg", "voice_bytes"),
            )
            for index, (kind, filename, mime, limit_name) in enumerate(cases):
                root = Path(directory) / f"attachments-{index}"
                store = AttachmentStore(_policy(root, **{limit_name: 4}))
                with self.assertRaisesRegex(MediaError, "size limit"):
                    await store.stage(
                        kind=kind,
                        telegram_file_id=f"file-{index}",
                        filename=filename,
                        mime_type=mime,
                        expected_size=None,
                        chunks=_chunks(b"1234", b"5"),
                    )
                self.assertEqual(tuple(root.iterdir()), ())

    async def test_stream_stops_one_byte_over_aggregate_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "attachments"
            store = AttachmentStore(
                _policy(root, max_total_bytes=5, document_bytes=10)
            )
            existing = await store.stage(
                kind="document",
                telegram_file_id="existing",
                filename="a.txt",
                mime_type="text/plain",
                expected_size=3,
                chunks=_chunks(b"123"),
            )
            with self.assertRaisesRegex(MediaError, "bundle size"):
                await store.stage(
                    kind="document",
                    telegram_file_id="new",
                    filename="b.txt",
                    mime_type="text/plain",
                    expected_size=None,
                    chunks=_chunks(b"12", b"3"),
                    existing=(existing,),
                )

    async def test_metadata_count_and_aggregate_caps_fail_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "attachments"
            store = AttachmentStore(
                _policy(root, max_items=1, max_total_bytes=6, document_bytes=6)
            )
            first = await store.stage(
                kind="document",
                telegram_file_id="file-3",
                filename="a.txt",
                mime_type="text/plain",
                expected_size=4,
                chunks=_chunks(b"1234"),
            )

            read = False

            async def forbidden_chunks():
                nonlocal read
                read = True
                yield b"x"

            with self.assertRaisesRegex(MediaError, "item limit"):
                await store.stage(
                    kind="document",
                    telegram_file_id="file-4",
                    filename="b.txt",
                    mime_type="text/plain",
                    expected_size=1,
                    chunks=forbidden_chunks(),
                    existing=(first,),
                )
            self.assertFalse(read)

            aggregate_store = AttachmentStore(
                _policy(root, max_items=10, max_total_bytes=4, document_bytes=6)
            )
            with self.assertRaisesRegex(MediaError, "bundle size"):
                await aggregate_store.stage(
                    kind="document",
                    telegram_file_id="file-5",
                    filename="b.txt",
                    mime_type="text/plain",
                    expected_size=1,
                    chunks=forbidden_chunks(),
                    existing=(first,),
                )
            self.assertFalse(read)

    async def test_signature_extension_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AttachmentStore(_policy(Path(directory) / "attachments"))
            with self.assertRaisesRegex(MediaError, "type"):
                await store.stage(
                    kind="photo",
                    telegram_file_id="photo-1",
                    filename="photo.png",
                    mime_type="image/png",
                    expected_size=6,
                    chunks=_chunks(b"notpng"),
                )
            with self.assertRaisesRegex(MediaError, "type"):
                await store.stage(
                    kind="document",
                    telegram_file_id="doc-1",
                    filename="manual.pdf",
                    mime_type="application/pdf",
                    expected_size=5,
                    chunks=_chunks(b"MZbad"),
                )


class GroqTranscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_endpoint_model_and_key_is_read_only_at_call_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "groq-key"
            key.write_text("sentinel-secret\n")
            key.chmod(0o600)
            audio = root / "voice.mp3"
            audio.write_bytes(b"ID3voice")
            client = FakeClient()

            async def no_ffmpeg(*args, **kwargs):
                raise AssertionError("supported MP3 must not use ffmpeg")

            transcriber = GroqTranscriber(
                key,
                client=client,
                create_subprocess=no_ffmpeg,
            )
            result = await transcriber.transcribe(audio)

            self.assertEqual(result, "transcribed request")
            url, request = client.calls[0]
            self.assertEqual(
                url,
                "https://api.groq.com/openai/v1/audio/transcriptions",
            )
            self.assertEqual(request["data"]["model"], "whisper-large-v3-turbo")
            self.assertEqual(request["headers"]["Authorization"], "Bearer sentinel-secret")

    async def test_raw_aac_uses_exact_ffmpeg_and_failures_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "groq-key"
            key.write_text("sentinel-secret")
            key.chmod(0o600)
            audio = root / "voice.aac"
            audio.write_bytes(b"\xff\xf1voice")
            calls = []

            class Process:
                returncode = 0

                async def communicate(self):
                    return b"", b""

            async def ffmpeg(*argv, **kwargs):
                calls.append((argv, kwargs))
                Path(argv[-1]).write_bytes(b"RIFF....WAVEdata")
                return Process()

            transcriber = GroqTranscriber(
                key,
                client=FakeClient(),
                create_subprocess=ffmpeg,
            )
            await transcriber.transcribe(audio)
            argv = calls[0][0]
            self.assertEqual(
                argv[:8],
                (
                    "/usr/bin/ffmpeg",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(audio),
                    "-vn",
                    "-acodec",
                    "pcm_s16le",
                ),
            )

            request = httpx.Request("POST", "https://api.groq.com")
            native = root / "voice.mp3"
            native.write_bytes(b"ID3voice")
            failing = GroqTranscriber(
                key,
                client=FakeClient(httpx.ConnectError("sentinel-secret", request=request)),
            )
            with self.assertRaises(MediaError) as caught:
                await failing.transcribe(native)
            self.assertNotIn("sentinel-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
