"""Independent, read-only diagnostics with bounded, secret-free results."""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from telegram_pi_bot.config import BotConfig


CHECK_TIMEOUT_SECONDS = 10
SUPPORTED_PI_VERSIONS = frozenset({"1.0.2"})


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


class Doctor:
    def __init__(self, config: BotConfig, runtime, *, telegram=None, delivery=None,
                 poller_lock=None, command_runner=None,
                 expected_poller_pid: int | None = None) -> None:
        self.config = config
        self.runtime = runtime
        self.telegram = telegram
        self.delivery = delivery
        self.poller_lock = poller_lock
        self.command_runner = command_runner or _command_available
        self.expected_poller_pid = expected_poller_pid

    async def run(self) -> tuple[CheckResult, ...]:
        snapshot = None

        async def metadata():
            nonlocal snapshot
            if snapshot is None:
                snapshot = await self.runtime.inspect(None)
            return snapshot

        def identity():
            user_id = self.config.allowed_user_id
            if type(user_id) is not int or user_id <= 0 or self.config.private_chat_only is not True:
                raise ValueError
            return "configured positive identity with private chat only"

        def config_files():
            BotConfig.validate_secret_file(self.config.config_path)
            BotConfig.validate_secret_file(self.config.secrets_env_file)
            return "config and secrets are owned regular files, mode 0600"

        def token():
            if not self.config.telegram_bot_token.strip():
                raise ValueError
            return "token present (value withheld)"

        async def telegram():
            if self.telegram is None or not await self.telegram.check_identity():
                raise ValueError
            return "Telegram bot identity reachable"

        async def pi_version():
            version = await self.command_runner((str(self.config.pi_cli), "--version"))
            if version not in SUPPORTED_PI_VERSIONS:
                raise ValueError
            return f"Pi {version} is explicitly supported"

        async def rpc():
            await metadata()
            return "metadata RPC succeeded without prompting"

        async def models():
            live = await metadata()
            allowed = all(
                model.provider == "ollama" or (model.provider, model.model_id) == self.config.agy_model
                for model in live.models
            )
            if not allowed or not any((model.provider, model.model_id) == self.config.default_model for model in live.models):
                raise ValueError
            if any(ready is not True for ready in live.provider_readiness.values()):
                raise ValueError
            return f"{len(live.models)} allowed models advertised; bot default present (no inference probe)"

        def sessions():
            root = self.config.sessions_dir
            if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
                raise ValueError
            return "native session directory readable; terminal sharing is sequential"

        async def skills():
            live = await metadata()
            return f"{len(live.skills)} live skills discovered"

        def state():
            _private_directory(self.config.state_dir)
            path = self.config.state_dir / "control.sqlite3"
            if path.exists():
                with _database(path):
                    pass
            return "private state storage available"

        def attachments():
            _private_directory(self.config.state_dir / "attachments")
            return "private attachment storage available"

        def artifacts():
            root = self.delivery.staging_root if self.delivery is not None else self.config.state_dir / "artifacts/staging"
            _private_directory(root)
            return "private artifact storage available"

        def groq():
            BotConfig.validate_secret_file(self.config.groq_key_file)
            if self.config.groq_key_file.stat().st_size == 0:
                raise ValueError
            return "Groq key file present, owned, mode 0600 (value withheld)"

        async def ffmpeg():
            if not await self.command_runner(("/usr/bin/ffmpeg", "-version")):
                raise ValueError
            return "ffmpeg version command succeeded"

        def poller():
            if self.poller_lock is not None and self.poller_lock.held:
                return "this process owns the poller guard"
            path = self.config.state_dir / "bot.lock"
            expected_pid = self.expected_poller_pid
            if expected_pid is not None and expected_pid <= 0:
                raise ValueError
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except FileNotFoundError:
                if expected_pid is not None:
                    raise ValueError
                return "no competing poller guard found"
            try:
                details = os.fstat(descriptor)
                if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                        or stat.S_IMODE(details.st_mode) != 0o600):
                    raise ValueError
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if expected_pid is None:
                        raise
                    value = os.read(descriptor, 64).decode("ascii").strip()
                    if value != str(expected_pid):
                        raise ValueError
                    return f"expected poller guard held by PID {expected_pid}"
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    if expected_pid is not None:
                        raise ValueError
            finally:
                os.close(descriptor)
            return "no competing poller guard found"

        def outbound():
            path = self.delivery.path if self.delivery is not None else self.config.state_dir / "delivery.sqlite3"
            if not path.exists():
                return "outbound queue not yet initialized"
            with _database(path) as database:
                pending = database.execute("SELECT count(*) FROM deliveries WHERE status = 'pending'").fetchone()[0]
                uncertain = database.execute("SELECT count(*) FROM deliveries WHERE status = 'uncertain' AND last_error != 'delivery_expired'").fetchone()[0]
                expired = database.execute("SELECT count(*) FROM deliveries WHERE last_error = 'delivery_expired'").fetchone()[0]
            return f"pending={pending}; uncertain={uncertain}; expired={expired}; no delivery attempted"

        checks = (
            ("identity", identity, "configure the exact authorized private identity"),
            ("config", config_files, "restore owned config/secrets files with mode 0600"),
            ("token", token, "enter the bot token locally"),
            ("telegram", telegram, "check Telegram token and connectivity"),
            ("pi_version", pi_version, "install the explicitly supported Pi 1.0.2"),
            ("rpc", rpc, "check Pi metadata RPC availability"),
            ("models", models, "restore the allowed live catalog and bot default"),
            ("sessions", sessions, "check native session directory access"),
            ("skills", skills, "check native skill discovery"),
            ("state", state, "check private state permissions and SQLite integrity"),
            ("attachments", attachments, "check private attachment storage permissions"),
            ("artifacts", artifacts, "check private artifact staging permissions"),
            ("groq", groq, "restore a nonempty owned Groq key file with mode 0600"),
            ("ffmpeg", ffmpeg, "check /usr/bin/ffmpeg installation"),
            ("poller", poller, "stop the competing bot poller"),
            ("outbound", outbound, "check outbound SQLite integrity"),
        )
        results = []
        for name, check, correction in checks:
            try:
                async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
                    detail = await check() if inspect.iscoroutinefunction(check) else await _run_sync(check)
                results.append(CheckResult(name, True, detail))
            except Exception:
                results.append(CheckResult(name, False, correction))
        return tuple(results)


async def _run_sync(check):
    """Daemon isolation keeps a timed-out syscall out of executor shutdown."""
    completed = threading.Event()
    outcome = []

    def worker():
        try:
            value = (check(), None)
        except Exception as error:
            value = (None, error)
        outcome.append(value)
        completed.set()

    threading.Thread(target=worker, daemon=True).start()
    while not completed.is_set():
        await asyncio.sleep(0.01)
    value, error = outcome[0]
    if error is not None:
        raise error
    return value


def _private_directory(path: Path) -> None:
    details = path.lstat()
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700
            or not os.access(path, os.R_OK | os.W_OK | os.X_OK)):
        raise ValueError


def validate_storage_paths(config: BotConfig) -> None:
    """Reject unsafe existing storage before constructors normalize permissions."""
    try:
        for path in (config.state_dir, config.state_dir / "attachments",
                     config.state_dir / "artifacts", config.state_dir / "artifacts/staging"):
            if path.exists() or path.is_symlink():
                _private_directory(path)
        for name in ("control.sqlite3", "delivery.sqlite3"):
            path = config.state_dir / name
            if path.exists() or path.is_symlink():
                BotConfig.validate_secret_file(path)
    except (OSError, ValueError):
        raise RuntimeError("private state storage permissions are unsafe") from None


@contextmanager
def _database(path: Path):
    deadline = time.monotonic() + CHECK_TIMEOUT_SECONDS
    BotConfig.validate_secret_file(path)
    database = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=max(0, deadline - time.monotonic()))
    database.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1_000)
    timer = threading.Timer(max(0, deadline - time.monotonic()), database.interrupt)
    timer.daemon = True
    timer.start()
    try:
        if database.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError
        yield database
    finally:
        timer.cancel()
        timer.join()
        database.close()


async def _command_available(argv: tuple[str, ...]) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await process.communicate()
        if process.returncode != 0 or len(stdout) > 64 * 1024:
            return ""
        return stdout.decode("utf-8", errors="strict").strip()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
