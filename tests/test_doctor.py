from __future__ import annotations

import os
import asyncio
import io
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telegram_pi_bot.doctor import Doctor
from telegram_pi_bot.app import BotApplication, ProcessLock
from tests.fakes import FakeRuntime, FakeSystem, mixed_provider_models
from tests.test_config import _config_text


class DoctorTests(unittest.IsolatedAsyncioTestCase):
    async def test_doctor_checks_the_configured_pi_executable(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        config = replace(system.app.config, pi_cli=system.root / "custom-pi")
        runner = AsyncMock(return_value="1.0.2")
        checks = await Doctor(
            config, system.pi, telegram=system.telegram,
            delivery=system.app.delivery, command_runner=runner,
        ).run()
        self.assertTrue(next(check for check in checks if check.name == "pi_version").ok)
        runner.assert_any_await((str(config.pi_cli), "--version"))
        self.assertNotIn(("/usr/bin/pi", "--version"), [call.args[0] for call in runner.await_args_list])

    async def test_doctor_accepts_only_the_explicitly_tested_pi_version(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        runner = AsyncMock(return_value="1.0.2")
        checks = await Doctor(
            system.app.config,
            system.pi,
            telegram=system.telegram,
            delivery=system.app.delivery,
            command_runner=runner,
        ).run()
        version = next(check for check in checks if check.name == "pi_version")
        self.assertTrue(version.ok)
        self.assertIn("1.0.2", version.detail)

        runner.return_value = "1.0.3"
        checks = await Doctor(
            system.app.config,
            system.pi,
            telegram=system.telegram,
            delivery=system.app.delivery,
            command_runner=runner,
        ).run()
        version = next(check for check in checks if check.name == "pi_version")
        self.assertFalse(version.ok)

    def test_cli_exits_with_all_results_even_when_sync_check_stays_blocked(self):
        child = textwrap.dedent("""
            import json, os, sys, tempfile, threading
            from pathlib import Path
            from unittest.mock import AsyncMock, patch
            from telegram_pi_bot.__main__ import main
            from telegram_pi_bot.config import BotConfig
            from tests.fakes import FakeRuntime
            from tests.test_config import _config_text, _pi_settings

            with tempfile.TemporaryDirectory() as directory:
                agent = Path(directory) / ".pi/agent"
                agent.mkdir(parents=True)
                (agent / "settings.json").write_text(json.dumps(_pi_settings()))
                os.environ["PI_CODING_AGENT_DIR"] = str(agent)
                path = Path(directory) / "config.toml"
                path.write_text(_config_text(home=directory))
                path.chmod(0o600)
                original = BotConfig.validate_secret_file
                def blocked(file):
                    if file == path:
                        threading.Event().wait()
                    original(file)
                with patch("sys.argv", ["telegram-pi-bot", "doctor", "--config", str(path)]), \
                     patch("telegram_pi_bot.__main__._environment", return_value=dict(os.environ, TELEGRAM_BOT_TOKEN="TOKEN_SENTINEL")), \
                     patch("telegram_pi_bot.__main__.PiRuntime", return_value=FakeRuntime()), \
                     patch("telegram_pi_bot.telegram_adapter.TelegramAdapter.check_identity", AsyncMock(return_value=True)), \
                     patch("telegram_pi_bot.doctor._command_available", AsyncMock(return_value="1.0.2")), \
                     patch("telegram_pi_bot.doctor.CHECK_TIMEOUT_SECONDS", 0.03), \
                     patch.object(BotConfig, "validate_secret_file", blocked):
                    try:
                        main()
                    except SystemExit:
                        pass
            """)
        # run() kills and reaps the child on TimeoutExpired, including the RED case.
        result = subprocess.run([sys.executable, "-u", "-c", child], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 16)
        self.assertIn("config: FAIL", result.stdout)
        self.assertIn("ffmpeg: OK", result.stdout)
        self.assertNotIn("TOKEN_SENTINEL", result.stdout + result.stderr)

    def test_late_sync_completion_after_loop_close_has_no_unhandled_error(self):
        from telegram_pi_bot.doctor import _run_sync
        for fail in (False, True):
            release = threading.Event()
            workers = []
            def slow():
                workers.append(threading.current_thread())
                release.wait()
                if fail:
                    raise ValueError("PRIVATE_ERROR_SENTINEL")
                return "ok"
            async def bounded():
                with self.assertRaises(TimeoutError):
                    async with asyncio.timeout(0.03):
                        await _run_sync(slow)
            with patch("threading.excepthook") as unhandled:
                try:
                    asyncio.run(bounded())
                finally:
                    release.set()
                    for worker in workers:
                        worker.join(timeout=1)
                        self.assertFalse(worker.is_alive())
                unhandled.assert_not_called()

    def test_cli_doctor_missing_or_invalid_token_still_runs_all_checks(self):
        import json
        from telegram_pi_bot.__main__ import main
        from telegram_pi_bot.config import BotConfig, ConfigError
        from tests.test_config import _pi_settings
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        agent = Path(temporary.name) / ".pi/agent"
        agent.mkdir(parents=True)
        (agent / "settings.json").write_text(json.dumps(_pi_settings()))
        config_path = Path(temporary.name) / "diagnostic.toml"
        config_path.write_text(_config_text(home=temporary.name))
        config_path.chmod(0o600)
        for token in ("", "MALFORMED_TOKEN_SENTINEL"):
            output, error = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(agent)}), \
                 patch("sys.argv", ["telegram-pi-bot", "doctor", "--config", str(config_path)]), \
                 patch("telegram_pi_bot.__main__._environment", return_value=dict(os.environ, TELEGRAM_BOT_TOKEN=token)), \
                 patch("telegram_pi_bot.__main__.PiRuntime", return_value=FakeRuntime()), \
                 patch("telegram_pi_bot.telegram_adapter.TelegramAdapter.check_identity", AsyncMock(return_value=False)), \
                 patch("telegram_pi_bot.doctor._command_available", AsyncMock(return_value="1.0.2")), \
                 redirect_stdout(output), redirect_stderr(error):
                with self.assertRaises(SystemExit):
                    main()
            text = output.getvalue()
            self.assertEqual(len(text.splitlines()), 16)
            for name in ("token", "telegram", "pi_version", "rpc", "ffmpeg", "outbound"):
                self.assertIn(name + ":", text)
            self.assertIn("telegram: FAIL", text)
            if token:
                self.assertNotIn(token, text + error.getvalue())
            else:
                self.assertIn("token: FAIL", text)
                with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(agent)}), self.assertRaises(ConfigError):
                    BotConfig.load(config_path, {})

    async def test_missing_secret_environment_is_tolerated_only_for_doctor(self):
        from telegram_pi_bot.__main__ import _environment
        from telegram_pi_bot.config import ConfigError
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        with patch.dict(os.environ, {}, clear=True):
            secrets_path = system.root / "missing.env"
            self.assertNotIn("TELEGRAM_BOT_TOKEN", _environment(required=False, secrets_path=secrets_path))
            with self.assertRaises(ConfigError):
                _environment(secrets_path=secrets_path)

    async def test_slow_sync_check_times_out_off_loop_and_later_checks_finish(self):
        from telegram_pi_bot.config import BotConfig
        system = await FakeSystem.start(models=mixed_provider_models())
        self.addAsyncCleanup(system.close)
        config = replace(system.app.config, secrets_env_file=system.root / "secrets.env")
        for path in (config.config_path, config.secrets_env_file):
            path.write_text("private")
            path.chmod(0o600)
        original = BotConfig.validate_secret_file
        release, finished = threading.Event(), threading.Event()
        def slow(path):
            if path == config.config_path:
                release.wait(1)
                finished.set()
            original(path)
        with patch("telegram_pi_bot.doctor.CHECK_TIMEOUT_SECONDS", 0.03, create=True), patch.object(BotConfig, "validate_secret_file", slow):
            try:
                checks = await Doctor(config, system.pi, telegram=system.telegram, delivery=system.app.delivery, command_runner=AsyncMock(return_value="1.0.2")).run()
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                await asyncio.to_thread(finished.wait, 1)
        self.assertFalse(next(check for check in checks if check.name == "config").ok)
        self.assertTrue(next(check for check in checks if check.name == "ffmpeg").ok)
        self.assertEqual(len(checks), 16)

    async def test_sqlite_check_deadline_interrupts_worker_and_closes_connection(self):
        from telegram_pi_bot.doctor import _database
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        connect = sqlite3.connect
        finished = threading.Event()
        class SlowConnection:
            def __init__(self, *args, **values):
                self.database = connect(*args, **values)
            def __getattr__(self, name):
                return getattr(self.database, name)
            def execute(self, sql):
                if sql == "PRAGMA quick_check":
                    return self.database.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT count(*) FROM n")
                return self.database.execute(sql)
            def close(self):
                self.database.close()
                finished.set()
        def check():
            with _database(system.store.path):
                pass
        with patch("telegram_pi_bot.doctor.CHECK_TIMEOUT_SECONDS", 0.02, create=True), patch("telegram_pi_bot.doctor.sqlite3.connect", SlowConnection):
            with self.assertRaises(sqlite3.OperationalError):
                await asyncio.wait_for(asyncio.to_thread(check), 0.5)
        self.assertTrue(finished.is_set())

    async def test_independent_checks_redact_failures_and_never_prompt_or_mutate(self):
        system = await FakeSystem.start(models=mixed_provider_models(), skills=[("advisor", "Advisor")])
        self.addAsyncCleanup(system.close)
        token, key, error = "TOKEN_SENTINEL", "KEY_SENTINEL", "ERROR_SENTINEL"
        config = replace(system.app.config, telegram_bot_token=token, secrets_env_file=system.root / "secrets.env")
        for path, body in ((config.config_path, "config"), (config.secrets_env_file, token), (config.groq_key_file, key)):
            path.write_text(body)
            path.chmod(0o600)
        os.chmod(config.config_path, 0o644)
        system.pi.inspect = AsyncMock(side_effect=RuntimeError(error + token + key))
        runner = AsyncMock(return_value="1.0.2")
        checks = await Doctor(config, system.pi, delivery=system.app.delivery, telegram=system.telegram, command_runner=runner).run()
        names = [check.name for check in checks]
        for name in ("identity", "config", "token", "telegram", "pi_version", "rpc", "models", "sessions", "skills", "state", "attachments", "artifacts", "groq", "ffmpeg", "poller", "outbound"):
            self.assertIn(name, names)
        self.assertFalse(next(check for check in checks if check.name == "config").ok)
        self.assertFalse(next(check for check in checks if check.name == "rpc").ok)
        self.assertTrue(next(check for check in checks if check.name == "ffmpeg").ok)
        for sentinel in (token, key, error):
            self.assertNotIn(sentinel, repr(checks))
        self.assertEqual(system.pi.prompt_count, 0)
        self.assertEqual(system.store.load(system.CHAT_ID).version, 0)
        self.assertIn(("/usr/bin/pi", "--version"), [call.args[0] for call in runner.await_args_list])
        self.assertIn(("/usr/bin/ffmpeg", "-version"), [call.args[0] for call in runner.await_args_list])

    async def test_hidden_doctor_uses_independent_structured_checks(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        await system.telegram.command("/doctor")
        text = system.telegram.final_texts[-1]
        self.assertIn("config:", text)
        self.assertIn("outbound:", text)
        self.assertNotIn("checks passed", text)
        self.assertEqual(system.pi.prompt_count, 0)

    async def test_poller_check_distinguishes_owned_guard_from_competing_process(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        lock = ProcessLock(system.root / "bot.lock")
        lock.acquire()
        try:
            doctor = Doctor(system.app.config, system.pi, telegram=system.telegram, delivery=system.app.delivery, command_runner=AsyncMock(return_value="1.0.2"))
            checks = await doctor.run()
            self.assertFalse(next(check for check in checks if check.name == "poller").ok)
            doctor.poller_lock = lock
            checks = await doctor.run()
            self.assertTrue(next(check for check in checks if check.name == "poller").ok)
        finally:
            lock.release()

    async def test_missing_default_and_unsafe_groq_fail_without_model_calls(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        system.app.config.groq_key_file.write_text("GROQ_SENTINEL")
        system.app.config.groq_key_file.chmod(0o644)
        checks = await Doctor(system.app.config, system.pi, telegram=system.telegram, delivery=system.app.delivery, command_runner=AsyncMock(return_value="1.0.2")).run()
        for name in ("models", "groq"):
            self.assertFalse(next(check for check in checks if check.name == name).ok)
        self.assertNotIn("GROQ_SENTINEL", repr(checks))
        self.assertEqual(system.pi.prompt_count, 0)

    async def test_startup_rejects_unsafe_existing_storage_before_chmod_or_polling(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        await system.app.stop()
        system.store.path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "storage permissions"):
            BotApplication.from_config(system.app.config)
        self.assertEqual(system.store.path.stat().st_mode & 0o777, 0o644)

    async def test_readiness_fails_before_recovery_or_any_prompt(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        await system.app.stop()
        system.app.validate_readiness = True
        system.app.doctor.command_runner = AsyncMock(return_value="1.0.2")
        with self.assertRaisesRegex(RuntimeError, "readiness failed"):
            await system.app.start()
        self.assertEqual(system.store.load(system.CHAT_ID).version, 0)
        self.assertEqual(system.pi.prompt_count, 0)
