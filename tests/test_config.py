import json
import tempfile
import unittest
from pathlib import Path

from telegram_pi_bot.config import BotConfig, ConfigError


class ConfigTests(unittest.TestCase):
    def test_config_accepts_positive_identity_and_resolves_home_paths(self):
        config = self._load(
            _config_text(user_id=123456789, home="~"),
            {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
            home=Path("/home/alice"),
        )

        self.assertEqual(config.allowed_user_id, 123456789)
        self.assertTrue(config.private_chat_only)
        self.assertEqual(config.cwd, Path("/home/alice"))
        self.assertEqual(config.pi_cli, Path("/usr/bin/pi"))
        self.assertEqual(config.state_dir, Path("/home/alice/.local/state/telegram-pi-bot"))
        self.assertEqual(config.groq_key_file, Path("/home/alice/.config/groq/api_key"))
        self.assertEqual(config.pi_agent_dir, Path("/home/alice/.pi/agent"))
        self.assertEqual(
            config.sessions_dir,
            Path("/home/alice/.pi/agent/sessions/--home-alice--"),
        )
        self.assertEqual(
            config.secrets_env_file,
            config.config_path.parent / "secrets.env",
        )

    def test_fixed_behavioral_values_remain_exact(self):
        config = self._load(_config_text(), {"TELEGRAM_BOT_TOKEN": "token-sentinel"})

        self.assertEqual(config.default_model, ("antigravity", "gemini-3.7-flash"))
        self.assertEqual(config.default_thinking, "high")
        self.assertEqual(config.agy_model, ("antigravity", "gemini-3.7-flash"))
        self.assertEqual(config.agy_thinking, "high")
        self.assertEqual(config.turn_timeout_seconds, 6 * 60 * 60)
        self.assertEqual(config.text_delay_seconds, 5)
        self.assertEqual(config.media_delay_seconds, 10)
        self.assertEqual(config.inbound_bundle_bytes, 50 * 1024 * 1024)
        self.assertEqual(config.outbound_artifacts_per_turn, 5)
        self.assertEqual(config.groq_base_url, "https://api.groq.com/openai/v1/")
        self.assertEqual(config.groq_model, "whisper-large-v3-turbo")

    def test_session_directory_precedence_matches_pi(self):
        # project sessionDir overrides user sessionDir
        project = {"sessionDir": "./project-sessions"}
        config = self._load(
            _config_text(user_id=123456789, home="/work/project"),
            {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
            home=Path("/home/alice"),
            user_settings={**_pi_settings(), "sessionDir": "~/user-sessions"},
            project_settings=project,
        )
        self.assertEqual(config.sessions_dir, Path("/work/project/project-sessions"))

        overridden = self._load(
            _config_text(user_id=123456789, home="/work/project"),
            {
                "TELEGRAM_BOT_TOKEN": "token-sentinel",
                "PI_CODING_AGENT_SESSION_DIR": "/srv/pi-sessions",
            },
            home=Path("/home/alice"),
        )
        self.assertEqual(overridden.sessions_dir, Path("/srv/pi-sessions"))

    def test_rejects_empty_token_without_echoing_it(self):
        with self.assertRaisesRegex(ConfigError, "TELEGRAM_BOT_TOKEN") as caught:
            self._load(_config_text(), {"TELEGRAM_BOT_TOKEN": "  "})
        self.assertNotIn("token-sentinel", str(caught.exception))

    def test_configured_paths_reject_resolved_root(self):
        cases = {
            'absolute dotdot cwd': _config_text().replace('cwd = "/home/alice"', 'cwd = "/tmp/.."'),
            'home dotdot cwd': _config_text().replace('cwd = "/home/alice"', 'cwd = "~/../.."'),
            'home double-slash dotdot cwd': _config_text().replace('cwd = "/home/alice"', 'cwd = "~//.."'),
            'dotdot state_dir': _config_text().replace(
                'state_dir = "~/.local/state/telegram-pi-bot"', 'state_dir = "~/../../"'
            ),
        }
        for label, contents in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    ConfigError, "absolute scoped path is required"
                ) as caught:
                    self._load(contents, {"TELEGRAM_BOT_TOKEN": "token-sentinel"}, home=Path("/home/alice"))
                self.assertNotIn("/home/alice", str(caught.exception))

    def test_session_directory_rejects_resolved_root(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "token-sentinel",
            "PI_CODING_AGENT_SESSION_DIR": "/..",
        }
        with self.assertRaisesRegex(ConfigError, "session directory"):
            self._load(
                _config_text(user_id=123456789, home="/work/project"),
                env,
                home=Path("/home/alice"),
            )
        project = {"sessionDir": "/.."}
        with self.assertRaisesRegex(ConfigError, "session directory"):
            self._load(
                _config_text(user_id=123456789, home="/work/project"),
                {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
                home=Path("/home/alice"),
                project_settings=project,
            )
        config = self._load(
            _config_text(user_id=123456789, home="/work/project"),
            {
                "TELEGRAM_BOT_TOKEN": "token-sentinel",
                "PI_CODING_AGENT_SESSION_DIR": "/srv/session-ok",
            },
            home=Path("/home/alice"),
        )
        self.assertEqual(config.sessions_dir, Path("/srv/session-ok"))

    def test_session_directory_accepts_plain_home_symbol(self):
        home_path = Path("/home/alice")
        expected = home_path.resolve()

        env_config = self._load(
            _config_text(user_id=123456789, home="/work/project"),
            {
                "TELEGRAM_BOT_TOKEN": "token-sentinel",
                "PI_CODING_AGENT_SESSION_DIR": "~",
            },
            home=home_path,
        )
        self.assertEqual(env_config.sessions_dir, expected)

        user_config = self._load(
            _config_text(user_id=123456789, home="/work/project"),
            {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
            home=home_path,
            user_settings={**_pi_settings(), "sessionDir": "~"},
        )
        self.assertEqual(user_config.sessions_dir, expected)

        project_config = self._load(
            _config_text(user_id=123456789, home="/work/project"),
            {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
            home=home_path,
            project_settings={"sessionDir": "~"},
        )
        self.assertEqual(project_config.sessions_dir, expected)

    def test_configured_home_prefix_is_preserved(self):
        config = self._load(
            _config_text(user_id=123456789, home="~//etc"),
            {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
            home=Path("/home/alice"),
        )
        self.assertEqual(config.cwd, Path("/home/alice/etc"))
        self.assertEqual(config.sessions_dir, Path("/home/alice/.pi/agent/sessions/--home-alice-etc--"))

    def test_symlinked_cwd_is_canonicalized_for_sessions(self):
        import re

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real_work = root / "real-work"
            real_work.mkdir()
            alias_work = root / "alias-work"
            alias_work.symlink_to(real_work)

            config = self._load(
                _config_text(user_id=123456789, home=str(alias_work)),
                {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
                home=root,
            )
            self.assertEqual(config.cwd, real_work.resolve())
            encoded = "--" + re.sub(r"[/\\:]", "-", str(real_work.resolve()).lstrip("/\\")) + "--"
            self.assertEqual(
                config.sessions_dir,
                (config.pi_agent_dir / "sessions" / encoded).resolve(),
            )

    def test_rejects_non_positive_or_non_integer_identity(self):
        cases = {
            "zero": "0",
            "negative": "-1",
            "string": '"123456789"',
        }
        for label, replacement in cases.items():
            text = _config_text().replace(
                "allowed_user_id = 123456789", f"allowed_user_id = {replacement}"
            )
            with self.subTest(label=label), self.assertRaisesRegex(
                ConfigError, "allowed_user_id"
            ) as caught:
                self._load(
                    text, {"TELEGRAM_BOT_TOKEN": "token-sentinel"}, home=Path("/home/alice")
                )
            self.assertNotIn(replacement.replace('"', ""), str(caught.exception))

    def test_rejects_unsafe_or_unusable_configured_paths(self):
        cases = {
            "relative cwd": _config_text().replace('cwd = "/home/alice"', 'cwd = "work/project"'),
            "empty cwd": _config_text().replace('cwd = "/home/alice"', 'cwd = ""'),
            "relative state_dir": _config_text().replace(
                'state_dir = "~/.local/state/telegram-pi-bot"', 'state_dir = "state"'
            ),
            "root state_dir": _config_text().replace(
                'state_dir = "~/.local/state/telegram-pi-bot"', 'state_dir = "/"'
            ),
            "empty pi_cli": _config_text().replace('pi_cli = "/usr/bin/pi"', 'pi_cli = ""'),
            "empty groq key": _config_text().replace(
                'groq_key_file = "~/.config/groq/api_key"', 'groq_key_file = ""'
            ),
        }
        for label, contents in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ConfigError, "paths\\.") as caught:
                    self._load(contents, {"TELEGRAM_BOT_TOKEN": "token-sentinel"})
                self.assertNotIn("work/project", str(caught.exception))

    def test_rejects_unknown_keys_and_non_exact_security_values(self):
        cases = {
            "group chats": _config_text().replace(
                "private_chat_only = true", "private_chat_only = false"
            ),
            "wrong model": _config_text().replace("qwen3.8-orcarouter:latest", "other"),
            "wrong delay": _config_text().replace("text_delay_seconds = 5", "text_delay_seconds = 6"),
            "wrong retention": _config_text().replace(
                "metadata_retention_seconds = 2592000", "metadata_retention_seconds = 60"
            ),
            "unknown key": _config_text().replace("[bot]", "[bot]\nextra = 1"),
        }
        for label, contents in cases.items():
            with self.subTest(label=label), self.assertRaises(ConfigError):
                self._load(contents, {"TELEGRAM_BOT_TOKEN": "token-sentinel"})

    def test_project_settings_override_user_defaults(self):
        config = self._load(
            _config_text(),
            {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
            user_settings={
                "defaultProvider": "antigravity",
                "defaultModel": "gemini-3.7-flash",
                "defaultThinkingLevel": "high",
            },
            project_settings={
                "defaultProvider": "ollama",
                "defaultModel": "glm-5.3:cloud",
                "defaultThinkingLevel": "low",
            },
        )

        self.assertEqual(config.default_model, ("ollama", "glm-5.3:cloud"))
        self.assertEqual(config.default_thinking, "low")

    def test_absent_thinking_uses_pi_medium_default(self):
        config = self._load(
            _config_text(),
            {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
            user_settings={
                "defaultProvider": "ollama",
                "defaultModel": "glm-5.3:cloud",
            },
        )

        self.assertEqual(config.default_thinking, "medium")

    def test_rejects_invalid_pi_default_settings(self):
        cases = {
            "missing provider": {"defaultModel": "glm-5.3:cloud"},
            "missing model": {"defaultProvider": "ollama"},
            "invalid thinking": {
                "defaultProvider": "ollama",
                "defaultModel": "glm-5.3:cloud",
                "defaultThinkingLevel": "ultra",
            },
            "disallowed provider": {
                "defaultProvider": "openrouter",
                "defaultModel": "openai/gpt-6.1-sol",
                "defaultThinkingLevel": "high",
            },
            "disallowed antigravity model": {
                "defaultProvider": "antigravity",
                "defaultModel": "claude-opus-4-6",
                "defaultThinkingLevel": "high",
            },
            "agy below high": {
                "defaultProvider": "antigravity",
                "defaultModel": "gemini-3.7-flash",
                "defaultThinkingLevel": "medium",
            },
        }
        for label, settings in cases.items():
            with self.subTest(label=label), self.assertRaises(ConfigError):
                self._load(
                    _config_text(),
                    {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
                    user_settings=settings,
                )

    def test_rejects_malformed_pi_settings(self):
        with self.assertRaisesRegex(ConfigError, "Pi settings"):
            self._load(
                _config_text(),
                {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
                user_settings="{",
            )

    def test_rejects_unsafe_or_symlinked_secret_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            key = root / "groq-key"
            key.write_text("sentinel\n", encoding="utf-8")
            key.chmod(0o644)
            with self.assertRaisesRegex(ConfigError, "mode 0600"):
                BotConfig.validate_secret_file(key)

            key.chmod(0o600)
            link = root / "linked-key"
            link.symlink_to(key)
            with self.assertRaisesRegex(ConfigError, "regular file"):
                BotConfig.validate_secret_file(link)

    def test_startup_validation_accepts_generic_runtime_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pi = root / "pi"
            pi.write_text("#!/bin/sh\n", encoding="utf-8")
            pi.chmod(0o700)
            path = root / "config.toml"
            path.write_text(_config_text(home=str(root), pi_cli=str(pi)), encoding="utf-8")
            path.chmod(0o600)
            secrets = root / "secrets.env"
            secrets.write_text("TELEGRAM_BOT_TOKEN=token\n", encoding="utf-8")
            secrets.chmod(0o600)
            groq = root / ".config/groq/api_key"
            groq.parent.mkdir(parents=True, exist_ok=True)
            groq.write_text("sentinel\n", encoding="utf-8")
            groq.chmod(0o600)
            settings = root / "settings.json"
            settings.write_text(json.dumps(_pi_settings()), encoding="utf-8")
            config = BotConfig.load(
                path,
                {"TELEGRAM_BOT_TOKEN": "token-sentinel"},
                pi_settings_path=settings,
                project_settings_path=root / "missing-project-settings.json",
                home=root,
            )
            config.validate_startup_paths()

    def _load(
        self,
        contents: str,
        env: dict[str, str],
        *,
        user_settings: dict[str, object] | str | None = None,
        project_settings: dict[str, object] | None = None,
        home: Path | None = None,
    ) -> BotConfig:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "config.toml"
            path.write_text(contents, encoding="utf-8")
            settings = root / "settings.json"
            payload = _pi_settings() if user_settings is None else user_settings
            settings.write_text(
                payload if isinstance(payload, str) else json.dumps(payload),
                encoding="utf-8",
            )
            project = root / "project-settings.json"
            if project_settings is not None:
                project.write_text(json.dumps(project_settings), encoding="utf-8")
            return BotConfig.load(
                path,
                env,
                pi_settings_path=settings,
                project_settings_path=project,
                **({"home": home} if home is not None else {}),
            )


def _pi_settings() -> dict[str, object]:
    return {
        "defaultProvider": "antigravity",
        "defaultModel": "gemini-3.7-flash",
        "defaultThinkingLevel": "high",
    }


def _config_text(user_id: int = 123456789, home: str = "/home/alice", pi_cli: str = "/usr/bin/pi") -> str:
    return f'''[bot]
allowed_user_id = {user_id}
private_chat_only = true
text_delay_seconds = 5
media_delay_seconds = 10
ui_timeout_seconds = 600
turn_timeout_seconds = 21600

[paths]
cwd = "{home}"
pi_cli = "{pi_cli}"
state_dir = "~/.local/state/telegram-pi-bot"
groq_key_file = "~/.config/groq/api_key"

[models]
default_provider = "ollama"
default_id = "qwen3.8-orcarouter:latest"
agy_provider = "antigravity"
agy_id = "gemini-3.7-flash"
agy_thinking = "high"

[limits]
inbound_items = 10
inbound_bundle_bytes = 52428800
voice_bytes = 20971520
document_bytes = 20971520
photo_bytes = 10485760
outbound_artifacts = 5
outbound_total_bytes = 52428800
outbound_file_bytes = 20971520
outbound_image_bytes = 10485760
staging_retention_seconds = 86400
metadata_retention_seconds = 2592000
'''