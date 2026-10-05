from __future__ import annotations

import os
import hashlib
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OperationsTests(unittest.TestCase):
    def test_unit_uses_external_environment_and_active_release(self):
        unit = (ROOT / "ops/pi-telegram.service").read_text(encoding="utf-8")
        self.assertIn("WorkingDirectory=%h", unit)
        self.assertIn("EnvironmentFile=%h/.config/telegram-pi-bot/secrets.env", unit)
        self.assertIn("ExecStart=%h/.local/opt/telegram-pi-bot/current/", unit)
        self.assertNotIn("/home/", unit)
        self.assertNotIn("TELEGRAM_BOT_TOKEN=", unit)
        self.assertIn("Type=simple", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=15", unit)
        self.assertIn("StartLimitIntervalSec=0", unit)
        self.assertNotIn("network-online.target", unit)
        self.assertIn("[Install]", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_default_paths_derive_from_home(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            env = {
                "HOME": str(fixture.home),
                "PATH": os.environ["PATH"],
                "TPB_SYSTEMCTL": str(fixture.fakebin / "systemctl"),
                "TPB_LOGINCTL": str(fixture.fakebin / "loginctl"),
            }
            result = subprocess.run(
                [str(ROOT / "ops/manage.sh"), "status"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Current release: __ABSENT__", result.stdout)

            bad_env = dict(env, HOME="/")
            bad_result = subprocess.run(
                [str(ROOT / "ops/manage.sh"), "status"],
                cwd=ROOT,
                env=bad_env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(bad_result.returncode, 0)
            self.assertIn("HOME must be an absolute scoped directory", bad_result.stderr)

    def test_installer_is_clean_frozen_nonactivating_and_checksum_guarded(self):
        script = (ROOT / "ops/install-release.sh").read_text(encoding="utf-8")
        self.assertIn("git status --porcelain", script)
        self.assertIn("uv venv --python 3.13 --relocatable .venv", script)
        self.assertIn("uv sync --frozen", script)
        self.assertIn("python -m unittest discover", script)
        self.assertNotIn("live_runtime_probe.py", script)
        self.assertIn("ops/verify-unit.sh", script)
        self.assertIn("telegram_pi_bot/extensions/telegram_artifacts.ts", script)
        self.assertIn("sha256sum", script)
        self.assertIn("readlink -f -- .venv/bin/python", script)
        self.assertIn("sha256sum .venv/bin/python >>SHA256SUMS", script)
        self.assertNotIn("systemctl --user start", script)
        self.assertNotIn("ln -sfn", script)

        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            project["tool"]["setuptools"]["package-data"]["telegram_pi_bot"],
            ["extensions/*.ts"],
        )

    def test_deploy_switches_current_and_enables_user_unit(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            candidate = fixture.release("a" * 40)
            prior = fixture.release("b" * 40)
            fixture.current.symlink_to(prior)
            result = fixture.manage("deploy", "a" * 40)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(fixture.current.resolve(), candidate.resolve())
            calls = fixture.systemctl_calls.read_text(encoding="utf-8")
            self.assertIn("--user enable --now pi-telegram.service", calls)
            self.assertTrue(fixture.backup_exists())

    def test_deploy_waits_for_stable_main_pid_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            candidate = fixture.release("a" * 40)
            fixture.transient_identity.write_text("pending", encoding="utf-8")
            fixture.wait_attempts = "3"
            result = fixture.manage("deploy", "a" * 40)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(fixture.current.resolve(), candidate.resolve())
            self.assertEqual(fixture.transient_identity.read_text(encoding="utf-8"), "stable")

    def test_identity_timeout_restores_once_without_false_recovery_alarm(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            old = fixture.release("b" * 40)
            fixture.release("a" * 40)
            fixture.current.symlink_to(old)
            fixture.transient_identity.write_text("pending", encoding="utf-8")
            result = fixture.manage("deploy", "a" * 40)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(fixture.current.resolve(), old.resolve())
            self.assertEqual(
                result.stderr.count(
                    "Deployment failed; prior target, unit, enablement, and activity were restored."
                ),
                1,
            )
            self.assertNotIn("could not be fully restored", result.stderr)

    def test_failed_start_restores_previous_target_and_enablement(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            old = fixture.release("b" * 40)
            fixture.release("a" * 40)
            fixture.current.symlink_to(old)
            fixture.systemctl_failure.write_text("--user enable --now pi-telegram.service", encoding="utf-8")
            result = fixture.manage("deploy", "a" * 40)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(fixture.current.resolve(), old.resolve())
            calls = fixture.systemctl_calls.read_text(encoding="utf-8")
            self.assertIn("--user disable pi-telegram.service", calls)

    def test_lingering_and_file_modes_are_deploy_preconditions(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            fixture.release("a" * 40)
            fixture.config.chmod(0o644)
            result = fixture.manage("deploy", "a" * 40)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(fixture.current.exists())
            fixture.config.chmod(0o600)
            fixture.linger.write_text("no", encoding="utf-8")
            result = fixture.manage("deploy", "a" * 40)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(fixture.current.exists())

    def test_first_deploy_creates_only_private_runtime_directories(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            fixture.release("a" * 40)
            fixture.remove_state()
            result = fixture.manage("deploy", "a" * 40)
            self.assertEqual(result.returncode, 0, result.stderr)
            for path in (
                fixture.state_root,
                fixture.state_root / "attachments",
                fixture.state_root / "artifacts",
                fixture.state_root / "artifacts/staging",
            ):
                self.assertTrue(path.is_dir())
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            enabled_link = fixture.unit_dir / "default.target.wants/pi-telegram.service"
            self.assertTrue(enabled_link.is_symlink())
            rolled_back = fixture.manage("rollback")
            self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
            self.assertFalse(fixture.current.exists())
            self.assertFalse(enabled_link.exists())
            self.assertFalse(enabled_link.is_symlink())

    def test_rejects_nonexact_build_id_and_refuses_existing_poller(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            fixture.release("a" * 40)
            invalid = fixture.manage("deploy", "A" * 40)
            self.assertNotEqual(invalid.returncode, 0)
            fixture.systemctl_state.write_text("enabled active", encoding="utf-8")
            duplicate = fixture.manage("deploy", "a" * 40)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertFalse(fixture.current.exists())

    def test_deploy_refuses_to_stop_a_tracked_but_unrelated_active_process(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            old = fixture.release("b" * 40)
            fixture.release("a" * 40)
            fixture.current.symlink_to(old)
            fixture.systemctl_state.write_text("enabled active", encoding="utf-8")
            fixture.poller_arguments = ("/usr/bin/python3", "/tmp/unrelated.py")
            result = fixture.manage("deploy", "a" * 40)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(fixture.current.resolve(), old.resolve())
            calls = fixture.systemctl_calls.read_text(encoding="utf-8")
            self.assertNotIn("--user stop pi-telegram.service", calls)

    def test_rollback_restores_target_and_never_removes_native_sessions(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            old = fixture.release("b" * 40)
            new = fixture.release("a" * 40)
            fixture.current.symlink_to(new)
            fixture.create_backup(old, enabled=False)
            (fixture.unit_dir / "pi-telegram.service").write_text("active unit", encoding="utf-8")
            import re
            encoded = "--" + re.sub(r"[/\\:]", "-", str(fixture.home).lstrip("/\\")) + "--"
            native = fixture.home / f".pi/agent/sessions/{encoded}/native.jsonl"
            native.parent.mkdir(parents=True)
            native.write_text('{"type":"session"}\n', encoding="utf-8")
            result = fixture.manage("rollback")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(fixture.current.resolve(), old.resolve())
            self.assertTrue(native.is_file())
            calls = fixture.systemctl_calls.read_text(encoding="utf-8")
            self.assertIn("--user disable pi-telegram.service", calls)

    def test_rollback_never_hides_a_failed_restore_command(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            old = fixture.release("b" * 40)
            new = fixture.release("a" * 40)
            fixture.current.symlink_to(new)
            fixture.create_backup(old, enabled=False)
            fixture.systemctl_failure.write_text("--user daemon-reload", encoding="utf-8")
            result = fixture.manage("rollback")
            self.assertNotEqual(result.returncode, 0)

    def test_rollback_checks_prior_release_before_stopping_or_switching(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            old = fixture.release("b" * 40)
            new = fixture.release("a" * 40)
            fixture.current.symlink_to(new)
            fixture.create_backup(old, enabled=False)
            (old / "ops/pi-telegram.service").write_text("tampered unit", encoding="utf-8")
            result = fixture.manage("rollback")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(fixture.current.resolve(), new.resolve())
            calls = fixture.systemctl_calls.read_text(encoding="utf-8")
            self.assertNotIn("--user stop pi-telegram.service", calls)

    def test_rollback_rejects_a_missing_saved_unit_instead_of_installing_empty_unit(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            old = fixture.release("b" * 40)
            new = fixture.release("a" * 40)
            fixture.current.symlink_to(new)
            fixture.create_backup(old, enabled=False)
            (fixture.state_root / "deployment-previous/previous-unit").unlink()
            result = fixture.manage("rollback")
            self.assertNotEqual(result.returncode, 0)
            unit = fixture.unit_dir / "pi-telegram.service"
            self.assertFalse(unit.exists())

    def test_deploy_rejects_an_unrecordable_prior_enablement_state(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = OpsFixture(Path(raw))
            old = fixture.release("b" * 40)
            fixture.release("a" * 40)
            fixture.current.symlink_to(old)
            fixture.systemctl_state.write_text("indirect inactive", encoding="utf-8")
            result = fixture.manage("deploy", "a" * 40)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(fixture.current.resolve(), old.resolve())

    def test_installer_refuses_dirty_or_nonmatching_commit_and_keeps_current_inactive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "repo"
            install = root / "install"
            fakebin = root / "fakebin"
            project.mkdir()
            fakebin.mkdir()
            (project / "src").mkdir()
            (project / "src/app.py").write_text("pass\n", encoding="utf-8")
            (project / "ops").mkdir()
            (project / "ops/install-release.sh").write_text("placeholder\n", encoding="utf-8")
            (project / "ops/manage.sh").write_text("placeholder\n", encoding="utf-8")
            (project / "ops/pi-telegram.service").write_text("[Service]\n", encoding="utf-8")
            verify_unit = project / "ops/verify-unit.sh"
            verify_unit.write_text("#!/bin/sh\nsystemd-analyze --user verify ops/pi-telegram.service\n", encoding="utf-8")
            verify_unit.chmod(0o700)
            (project / "docs").mkdir()
            (project / "docs/ACCEPTANCE.md").write_text("evidence\n", encoding="utf-8")
            (project / "config").mkdir()
            (project / "config/config.toml.example").write_text("config\n", encoding="utf-8")
            for name in ("pyproject.toml", "uv.lock", "README.md", "SPEC.md", "LICENSE"):
                (project / name).write_text("placeholder\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            subprocess.run(["git", "-C", str(project), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(project), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            subprocess.run(["git", "-C", str(project), "commit", "-qm", "fixture"], check=True)
            build_id = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
            uv = fakebin / "uv"
            uv.write_text("""#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
if args and args[0] == 'venv':
    root = pathlib.Path(args[-1])
    binary = root / 'bin/telegram-pi-bot'
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text('#!/bin/sh\\nexit 0\\n')
    binary.chmod(0o700)
    interpreter = pathlib.Path(__file__).with_name('python-fixture')
    interpreter.write_text('#!/bin/sh\\nexit 0\\n')
    interpreter.chmod(0o700)
    (root / 'bin/python').symlink_to(interpreter)
elif args and args[0] == 'sync' and os.environ.get('VIRTUAL_ENV'):
    extension = pathlib.Path(os.environ['VIRTUAL_ENV']) / 'lib/python3.13/site-packages/telegram_pi_bot/extensions/telegram_artifacts.ts'
    extension.parent.mkdir(parents=True, exist_ok=True)
    extension.write_text('extension fixture\\n')
""", encoding="utf-8")
            uv.chmod(0o700)
            systemd_analyze = fakebin / "systemd-analyze"
            systemd_analyze.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            systemd_analyze.chmod(0o700)
            env = os.environ.copy()
            env.update({"TPB_PROJECT_ROOT": str(project), "TPB_INSTALL_ROOT": str(install),
                        "PATH": str(fakebin) + os.pathsep + env["PATH"]})

            mismatch = subprocess.run([str(ROOT / "ops/install-release.sh"), "f" * 40],
                                      cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertFalse((install / "current").exists())

            (project / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
            dirty = subprocess.run([str(ROOT / "ops/install-release.sh"), build_id],
                                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(dirty.returncode, 0)
            (project / "dirty.txt").unlink()

            installed = subprocess.run([str(ROOT / "ops/install-release.sh"), build_id],
                                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            release = install / "releases" / build_id
            self.assertEqual((release / "SOURCE_COMMIT").read_text().strip(), build_id)
            self.assertTrue((release / "SHA256SUMS").is_file())
            self.assertFalse((install / "current").exists())
            verify = subprocess.run(["sha256sum", "--check", "--quiet", "SHA256SUMS"],
                                    cwd=release, capture_output=True, text=True)
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertIn(".venv/bin/python", (release / "SHA256SUMS").read_text())
            (fakebin / "python-fixture").write_text("changed interpreter\n", encoding="utf-8")
            changed = subprocess.run(["sha256sum", "--check", "--quiet", "SHA256SUMS"],
                                     cwd=release, capture_output=True, text=True)
            self.assertNotEqual(changed.returncode, 0)


class OpsFixture:
    def __init__(self, root: Path):
        self.home = root / "home"
        self.install = self.home / ".local/opt/telegram-pi-bot"
        self.current = self.install / "current"
        self.config_root = self.home / ".config/telegram-pi-bot"
        self.state_root = self.home / ".local/state/telegram-pi-bot"
        self.unit_dir = self.home / ".config/systemd/user"
        self.project = root / "project"
        self.fakebin = root / "fakebin"
        self.config_root.mkdir(parents=True)
        self.state_root.mkdir(parents=True)
        self.config_root.chmod(0o700)
        self.state_root.chmod(0o700)
        self.unit_dir.mkdir(parents=True)
        self.project.mkdir()
        self.fakebin.mkdir()
        self.config = self.config_root / "config.toml"
        self.secrets = self.config_root / "secrets.env"
        self.config.write_text("config", encoding="utf-8")
        self.secrets.write_text("TELEGRAM_BOT_TOKEN=not-a-real-token\n", encoding="utf-8")
        self.config.chmod(0o600)
        self.secrets.chmod(0o600)
        self.linger = root / "linger"
        self.linger.write_text("yes", encoding="utf-8")
        self.poller = root / "poller"
        self.systemctl_calls = root / "systemctl.calls"
        self.systemctl_failure = root / "systemctl.fail"
        self.systemctl_state = root / "systemctl.state"
        self.transient_identity = root / "transient-identity"
        self.wait_attempts = "1"
        self.poller_arguments: tuple[str, ...] | None = None
        self._fake("systemctl", """import os, pathlib, shutil, sys
args = sys.argv[1:]
pathlib.Path(os.environ['TPB_TEST_SYSTEMCTL_CALLS']).open('a').write(' '.join(args)+'\\n')
failure = pathlib.Path(os.environ['TPB_TEST_SYSTEMCTL_FAILURE'])
if failure.exists() and failure.read_text().strip() == ' '.join(args):
    raise SystemExit(1)
state = pathlib.Path(os.environ['TPB_TEST_SYSTEMCTL_STATE'])
value = state.read_text().split() if state.exists() else ['not-found', 'inactive']
unit_dir = pathlib.Path(os.environ['TPB_TEST_UNIT_DIR'])
enabled_link = unit_dir / 'default.target.wants/pi-telegram.service'
proc = pathlib.Path(os.environ['TPB_PROC_ROOT']) / '4321'
def start_process():
    proc.mkdir(parents=True, exist_ok=True)
    transient = pathlib.Path(os.environ['TPB_TEST_TRANSIENT_IDENTITY'])
    arguments = ['(telegram-pi-bot)'] if transient.exists() and transient.read_text() != 'stable' else ['/usr/bin/python3', os.environ['TPB_TEST_LAUNCHER'], 'run', '--config', os.environ['TPB_TEST_CONFIG']]
    (proc / 'cmdline').write_bytes(('\\0'.join(arguments) + '\\0').encode())
if 'enable' in args:
    value[0] = 'enabled'
    enabled_link.parent.mkdir(parents=True, exist_ok=True)
    if enabled_link.exists() or enabled_link.is_symlink():
        enabled_link.unlink()
    enabled_link.symlink_to(unit_dir / 'pi-telegram.service')
    if '--now' in args:
        value[1] = 'active'
        start_process()
elif 'disable' in args:
    value[0] = 'disabled'
    if enabled_link.exists() or enabled_link.is_symlink():
        enabled_link.unlink()
elif 'mask' in args:
    value[0] = 'masked'
elif 'start' in args:
    value[1] = 'active'
    start_process()
elif 'stop' in args:
    value[1] = 'inactive'
    if proc.exists():
        shutil.rmtree(proc)
if any(command in args for command in ('enable', 'disable', 'mask', 'start', 'stop')):
    state.write_text(' '.join(value))
if 'is-enabled' in args:
    print(value[0])
    raise SystemExit(0 if value[0] == 'enabled' else 1)
if 'is-active' in args:
    if value[1] != 'active':
        raise SystemExit(3)
    if '--quiet' not in args:
        print('active')
if 'show' in args and 'MainPID' in args:
    transient = pathlib.Path(os.environ['TPB_TEST_TRANSIENT_IDENTITY'])
    if value[1] == 'active' and transient.exists():
        if transient.read_text() == 'pending':
            transient.write_text('seen')
        elif transient.read_text() == 'seen':
            transient.write_text('stable')
            start_process()
    print('4321' if value[1] == 'active' else '0')
raise SystemExit(0)
""")
        self._fake("loginctl", """import os, pathlib, sys
print('yes' if pathlib.Path(os.environ['TPB_TEST_LINGER']).read_text().strip() == 'yes' else 'no')
""")

    def remove_state(self) -> None:
        self.state_root.rmdir()

    def _fake(self, name: str, body: str) -> None:
        path = self.fakebin / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o700)

    def release(self, build_id: str) -> Path:
        release = self.install / "releases" / build_id
        cli = release / ".venv/bin/telegram-pi-bot"
        ops = release / "ops"
        cli.parent.mkdir(parents=True, exist_ok=True)
        ops.mkdir(parents=True, exist_ok=True)
        cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        cli.chmod(0o700)
        (ops / "pi-telegram.service").write_text(
            (ROOT / "ops/pi-telegram.service").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (release / "SOURCE_COMMIT").write_text(build_id + "\n", encoding="utf-8")
        manifest = []
        for path in sorted(release.rglob("*")):
            if path.is_file() and path.name != "SHA256SUMS":
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                manifest.append(f"{digest}  {path.relative_to(release)}\n")
        (release / "SHA256SUMS").write_text("".join(manifest), encoding="utf-8")
        return release

    def create_backup(self, target: Path, *, enabled: bool) -> None:
        backup = self.state_root / "deployment-previous"
        backup.mkdir(mode=0o700)
        (backup / "previous-target").write_text(str(target), encoding="utf-8")
        (backup / "previous-enabled").write_text("enabled" if enabled else "disabled", encoding="utf-8")
        (backup / "previous-active").write_text("inactive", encoding="utf-8")
        (backup / "previous-unit-kind").write_text("regular", encoding="utf-8")
        (backup / "previous-unit").write_text("unit backup", encoding="utf-8")
        (backup / "activated-target").write_text(str(self.current.readlink()), encoding="utf-8")

    def backup_exists(self) -> bool:
        return (self.state_root / "deployment-previous").is_dir()

    def manage(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "TPB_INSTALL_ROOT": str(self.install),
            "TPB_CONFIG_ROOT": str(self.config_root),
            "TPB_STATE_ROOT": str(self.state_root),
            "TPB_SYSTEMD_USER_DIR": str(self.unit_dir),
            "TPB_SYSTEMCTL": str(self.fakebin / "systemctl"),
            "TPB_LOGINCTL": str(self.fakebin / "loginctl"),
            "TPB_PROC_ROOT": str(self.root_proc()),
            "TPB_PROJECT_ROOT": str(self.project),
            "TPB_TEST_SYSTEMCTL_CALLS": str(self.systemctl_calls),
            "TPB_TEST_SYSTEMCTL_FAILURE": str(self.systemctl_failure),
            "TPB_TEST_SYSTEMCTL_STATE": str(self.systemctl_state),
            "TPB_TEST_TRANSIENT_IDENTITY": str(self.transient_identity),
            "TPB_TEST_UNIT_DIR": str(self.unit_dir),
            "TPB_TEST_LAUNCHER": str(self.current / ".venv/bin/telegram-pi-bot"),
            "TPB_TEST_CONFIG": str(self.config),
            "TPB_TEST_LINGER": str(self.linger),
            "TPB_TEST_POLLER": str(self.poller),
            "TPB_STABILITY_SECONDS": "0",
            "TPB_WAIT_ATTEMPTS": self.wait_attempts,
            "TPB_WAIT_SECONDS": "0",
        })
        if args[:1] == ("deploy",) and len(args) == 2:
            launcher = self.current / ".venv/bin/telegram-pi-bot"
            proc = self.root_proc() / "4321"
            proc.mkdir(parents=True, exist_ok=True)
            arguments = self.poller_arguments or (
                "/usr/bin/python3", str(launcher), "run", "--config", str(self.config)
            )
            (proc / "cmdline").write_bytes(("\0".join(arguments) + "\0").encode())
        return subprocess.run([str(ROOT / "ops/manage.sh"), *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=10)

    def root_proc(self) -> Path:
        return self.project / "fake-proc"
