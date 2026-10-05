from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from telegram_pi_bot.app import BotApplication, ProcessLock
from telegram_pi_bot.doctor import Doctor
from tests.fakes import FakeSystem


ROOT = Path(__file__).resolve().parents[1]


class ReadinessDocumentationTests(unittest.TestCase):
    def test_acceptance_ledger_separates_evidence_and_keeps_live_checks_open(self):
        ledger = (ROOT / "docs/ACCEPTANCE.md").read_text(encoding="utf-8")
        for heading in ("Automated and local evidence", "Installed Pi compatibility evidence",
                        "Service and release evidence", "Real private-chat evidence"):
            self.assertIn(heading, ledger)
        real_section = ledger.split("## Real private-chat evidence", 1)[1]
        self.assertRegex(real_section, r"(?m)^- \[ \]")
        self.assertNotRegex(real_section, r"(?m)^- \[x\]")
        self.assertIn("No automated or local runtime result proves Telegram delivery", ledger)

    def test_readiness_material_points_to_external_mode_restricted_secret(self):
        config = (ROOT / "config/config.toml.example").read_text(encoding="utf-8")
        unit = (ROOT / "ops/pi-telegram.service").read_text(encoding="utf-8")
        operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
        self.assertIn("~/.config/telegram-pi-bot/secrets.env", config)
        self.assertIn("mode 0600", config)
        self.assertIn("EnvironmentFile=%h/.config/telegram-pi-bot/secrets.env", unit)
        self.assertIn("TELEGRAM_BOT_TOKEN=<token>", operations)
        self.assertNotRegex(config + unit, r"(?m)^\s*TELEGRAM_BOT_TOKEN\s*=\s*[^<\n]")

    def test_spec_defines_configured_identity_and_portable_pi_paths(self):
        spec = (ROOT / "SPEC.md").read_text(encoding="utf-8")
        self.assertIn("configured positive Telegram user ID", spec)
        self.assertIn("PI_CODING_AGENT_DIR", spec)
        self.assertIn("PI_CODING_AGENT_SESSION_DIR", spec)
        self.assertIn("runs Pi unsandboxed", spec)

    def test_operations_cover_secure_setup_lifecycle_and_native_session_boundary(self):
        operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
        for text in ("BotFather", "ops/install-release.sh", "ops/manage.sh status",
                     "ops/manage.sh deploy", "ops/manage.sh rollback", "journalctl",
                     "reboot", "sequentially", "native Pi session"):
            self.assertIn(text, operations)


class PollerReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_composition_enables_packaged_artifact_tools(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        app = BotApplication.from_config(system.app.config)
        self.assertIsNotNone(app.runtime._artifact_policy)
        self.assertTrue(app.runtime._artifact_extension_path.is_file())

    async def test_doctor_distinguishes_expected_held_poller_lock(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        lock = ProcessLock(system.app.config.state_dir / "bot.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(lock.path.read_text(encoding="ascii").strip(), str(lock_pid()))

        async def poller_result(expected_pid=None):
            doctor = Doctor(
                system.app.config,
                system.pi,
                telegram=system.telegram,
                delivery=system.app.delivery,
                command_runner=AsyncMock(return_value="1.0.2"),
                expected_poller_pid=expected_pid,
            )
            results = await doctor.run()
            return next(result for result in results if result.name == "poller")

        held = await poller_result()
        self.assertFalse(held.ok)
        self.assertEqual(held.detail, "stop the competing bot poller")

        expected = await poller_result(expected_pid=lock_pid())
        self.assertTrue(expected.ok)
        self.assertIn("expected poller guard held", expected.detail)

        mismatch = await poller_result(expected_pid=lock_pid() + 1)
        self.assertFalse(mismatch.ok)

    async def test_symlinked_poller_lock_is_rejected_without_touching_target(self):
        system = await FakeSystem.start()
        self.addAsyncCleanup(system.close)
        target = system.root / "outside-lock-target"
        target.write_text("do-not-change", encoding="utf-8")
        path = system.app.config.state_dir / "bot.lock"
        path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "unsafe or unavailable"):
            ProcessLock(path).acquire()
        self.assertEqual(target.read_text(encoding="utf-8"), "do-not-change")

        doctor = Doctor(
            system.app.config,
            system.pi,
            telegram=system.telegram,
            delivery=system.app.delivery,
            command_runner=AsyncMock(return_value="1.0.2"),
        )
        results = await doctor.run()
        poller = next(result for result in results if result.name == "poller")
        self.assertFalse(poller.ok)


def lock_pid() -> int:
    import os

    return os.getpid()


if __name__ == "__main__":
    unittest.main()
