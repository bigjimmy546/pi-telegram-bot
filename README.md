# Telegram Pi Bot

A single-user Telegram front end for the Pi coding agent on Linux. It runs Pi as a full coding agent from a configured working directory, sharing Pi's native JSONL sessions, model configuration, and skill discovery while providing immediate reaction feedback, input bundling, and durable message delivery over Telegram.

> **WARNING:** Pi runs unsandboxed with the service account's normal operating system permissions. Telegram prompts can inspect, modify, or delete any file and run any shell command that the host user account can access. Deploy only for your own positive Telegram user ID in private chats with a dedicated BotFather token.

## Features

- **Native Pi sessions**: Seamless two-way compatibility with terminal Pi JSONL sessions.
- **Queue and stop semantics**: Immediate reaction lifecycle (eyes on ingress, success/failure reaction on completion), 5s/10s input bundling, explicit steering, and graceful aborts.
- **Media and voice**: Full voice transcription via Groq Whisper, normalized image payloads, and document staging.
- **Blocking UI relay**: Relays Pi's blocking `select` and `confirm` UI requests as Telegram interactive buttons.
- **Artifact delivery**: Synchronous Unix-domain socket tools (`send_file`, `send_image`) with cryptographic hash verification and strict allowed-root checks.
- **Durable delivery**: Resilient SQLite-backed queueing and state transitions across restarts.
- **Guarded deployment**: Immutable checksummed releases, atomic symlink activation, and guarded service rollback.

## Requirements

- **Linux** with `systemd --user` and user lingering enabled (`loginctl enable-linger`)
- **Python 3.13** and **uv**
- **Pi 1.0.2** installed at `/usr/bin/pi` (or configured path)
- **ffmpeg** (required for voice message audio normalization)
- **Groq API Key** (for Whisper voice transcription)
- **Dedicated Telegram Bot Token** from BotFather

## Setup

1. Create the configuration directory with private permissions:
   ```bash
   mkdir -p ~/.config/telegram-pi-bot
   chmod 0700 ~/.config/telegram-pi-bot
   ```

2. Copy and customize configuration:
   ```bash
   cp config/config.toml.example ~/.config/telegram-pi-bot/config.toml
   chmod 0600 ~/.config/telegram-pi-bot/config.toml
   # Edit allowed_user_id and paths in ~/.config/telegram-pi-bot/config.toml
   ```

3. Create the secret environment file (mode `0600`) and enter your BotFather token using a text editor (avoiding shell history):
   ```bash
   touch ~/.config/telegram-pi-bot/secrets.env
   chmod 0600 ~/.config/telegram-pi-bot/secrets.env
   # Edit ~/.config/telegram-pi-bot/secrets.env to set:
   # TELEGRAM_BOT_TOKEN=<your-bot-token>
   ```

4. Verify your configuration:
   ```bash
   uv run telegram-pi-bot check-config --config ~/.config/telegram-pi-bot/config.toml
   ```

## Testing

Run the offline verification test suite (excludes live model and Telegram network calls):

```bash
uv lock --check
uv sync --frozen
uv run python -m compileall -q src tests
uv run python -m unittest discover -s tests -v
bash -n ops/install-release.sh ops/manage.sh ops/verify-unit.sh
ops/verify-unit.sh
git diff --check
```

## Documentation & License

- [Specification](SPEC.md) — Full technical specification and security boundaries
- [Operations Guide](docs/OPERATIONS.md) — Deployment, service management, and rollback
- [Acceptance Evidence Ledger](docs/ACCEPTANCE.md) — Verification checklist
- [Security Policy](SECURITY.md) — Vulnerability reporting and unsandboxed permissions
- [MIT License](LICENSE)
