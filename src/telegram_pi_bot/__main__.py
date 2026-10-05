"""Command-line entry point for telegram-pi-bot."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from pathlib import Path

from telegram_pi_bot.app import BotApplication, ProcessLock
from telegram_pi_bot.config import BotConfig, ConfigError
from telegram_pi_bot.doctor import Doctor
from telegram_pi_bot.pi_runtime import PiRuntime
from telegram_pi_bot.telegram_adapter import TelegramAdapter


DEFAULT_CONFIG = Path.home() / ".config/telegram-pi-bot/config.toml"


def _secrets_path(config_path: Path) -> Path:
    return config_path.parent / "secrets.env"


def main() -> None:
    parser = argparse.ArgumentParser(prog="telegram-pi-bot")
    parser.add_argument("command", choices=("run", "doctor", "check-config"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--expected-poller-pid", type=int)
    arguments = parser.parse_args()
    if arguments.expected_poller_pid is not None and arguments.command != "doctor":
        parser.error("--expected-poller-pid is only valid with doctor")
    try:
        diagnostic = arguments.command == "doctor"
        config = BotConfig.load(
            arguments.config,
            _environment(
                secrets_path=_secrets_path(arguments.config),
                required=not diagnostic,
            ),
            require_token=not diagnostic,
        )
        if arguments.command == "doctor":
            asyncio.run(_doctor(config, expected_poller_pid=arguments.expected_poller_pid))
            return
        config.validate_startup_paths()
        if arguments.command == "check-config":
            print("configuration ok")
            return
        asyncio.run(_run(config))
    except (ConfigError, RuntimeError, ValueError) as error:
        parser.exit(1, f"telegram-pi-bot: {error}\n")


async def _doctor(config: BotConfig, *, expected_poller_pid: int | None = None) -> None:
    async def dispatch(action):
        raise RuntimeError("diagnostics cannot dispatch")

    telegram = TelegramAdapter(config, dispatch)
    checks = await Doctor(
        config,
        PiRuntime(config),
        telegram=telegram,
        expected_poller_pid=expected_poller_pid,
    ).run()
    for check in checks:
        print(f"{check.name}: {'OK' if check.ok else 'FAIL'} — {check.detail}")
    if any(not check.ok for check in checks):
        raise RuntimeError("doctor found failed checks")


async def _run(config: BotConfig) -> None:
    lock = ProcessLock(config.state_dir / "bot.lock")
    lock.acquire()
    app = BotApplication.from_config(config)
    app.poller_lock = lock
    telegram = app.telegram.build_application()
    updater = telegram.updater
    if updater is None:
        lock.release()
        raise RuntimeError("Telegram polling is unavailable")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stopped.set)
    try:
        async with telegram:
            try:
                await app.start()
                await app.telegram.set_commands()
                await telegram.start()
                await updater.start_polling(drop_pending_updates=False)
                await stopped.wait()
            finally:
                if updater.running:
                    await updater.stop()
                await app.stop()
                if telegram.running:
                    await telegram.stop()
    except Exception:
        raise RuntimeError("Telegram polling failed; run doctor for redacted checks.") from None
    finally:
        lock.release()


def _environment(*, required: bool = True, secrets_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    if environment.get("TELEGRAM_BOT_TOKEN", "").strip():
        return environment
    try:
        BotConfig.validate_secret_file(secrets_path)
        lines = secrets_path.read_text(encoding="utf-8").splitlines()
    except ConfigError:
        if not required:
            return environment
        raise
    except (OSError, UnicodeError) as error:
        if not required:
            return environment
        raise ConfigError("secrets file: unreadable") from error
    for line in lines:
        if not line or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator and name == "TELEGRAM_BOT_TOKEN":
            environment[name] = value.strip()
            break
    return environment


if __name__ == "__main__":
    main()
