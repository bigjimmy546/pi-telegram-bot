# Operations

This runbook operates the dedicated `pi-telegram.service` user unit. Pi runs with the service account's normal operating system permissions and native sessions, so use Telegram and terminal Pi sequentially for a shared session.

## Initial setup

1. Create `~/.config/telegram-pi-bot` with mode `0700`.
2. Copy `config/config.toml.example` to
   `~/.config/telegram-pi-bot/config.toml` and set its mode to `0600`.
   Configure your positive Telegram `allowed_user_id` and desired working directory.
3. Create `~/.config/telegram-pi-bot/secrets.env` with mode `0600`
   and enter the dedicated BotFather token locally as
   `TELEGRAM_BOT_TOKEN=<token>`. Do not put it in chat, the repository, a
   command argument, or shell history. Do not reuse an existing bot token.
4. In BotFather, create a dedicated bot and set its command menu to
   `/new`, `/sessions`, `/use`, `/model`, `/thinking`, `/skill`, `/compact`,
   `/status`, `/stop`, `/usage`, and `/help`. `/start`, `/clear`, `/reset`,
   and `/doctor` are accepted but hidden. The bot only accepts the configured
   positive Telegram user ID in a private chat.

Configuration and secrets stay outside the repository. Startup and deployment
refuse unsafe file modes; diagnostics must never display the token.

## Build, deploy, and inspect

From the repository root, finish the frozen local gate and any required
pre-deploy review. Then build the requested commit without activating it:

```sh
ops/install-release.sh "$(git rev-parse HEAD)"
ops/manage.sh status
ops/manage.sh deploy "$(git rev-parse HEAD)"
```

Before any deploy, manual restart, or reboot, confirm `/status` shows no active
turn or session operation. Stopping the bot during a turn aborts that turn and
can leave its outcome uncertain.

The installer accepts a clean, exact 40-character commit ID, runs the offline
checks, copies tracked runtime material into a new release, creates its frozen
environment, verifies the unit against that staged launcher, and records file
checksums. It leaves the active `current` link unchanged. Deployment requires
safe config and secret modes, user lingering enabled (`loginctl enable-linger`),
a successful doctor check, and a single-poller state. It records the prior
release link, unit, and enablement state before activation.

Installation does not launch Pi, send a model prompt, or contact Telegram.
It may download Python and locked packages through uv. Installed-Pi probes in
`tests/live_runtime_probe.py` are separate, opt-in checks for the documented
fixture environment (`/usr/bin/pi`, the account's home directory, and
`ollama/qwen3.8-orcarouter:latest`). `--local-text` and
`--artifact-extension-only` send model prompts; `--offline` does not prevent
those provider calls. Deployment's doctor uses the configured Pi executable
and checks Telegram connectivity before activation.

The Python executable is pinned to its resolved interpreter path and included
in the checksum manifest. Keep that external Python installation available:
the interpreter's standard library and system libraries are host dependencies,
not bundled immutable release content.

`status` reports the active release and service health without exposing secret
values. Inspect journal output through application logging:

```sh
systemctl --user status pi-telegram.service
journalctl --user -u pi-telegram.service --since today
```

The service starts with the user manager after reboot. After a
restart or reboot, verify `systemctl --user is-active pi-telegram.service`,
run `ops/manage.sh status`, and check `/doctor` in the authorized private chat.
Do not infer Telegram delivery from a healthy systemd unit.
After activation, `systemd-analyze --user verify ops/pi-telegram.service`
verifies the untouched production unit against the now-existing `current`
launcher. Before activation, use `ops/verify-unit.sh`; direct verification of
the production unit correctly fails while `current` is intentionally absent.

## Rollback and recovery

Run `ops/manage.sh rollback` to restore the exact previous release target,
unit, and enabled/disabled state recorded by deployment. Rollback does not
delete releases or touch `.pi` sessions. Verify status and the user journal
afterward. If the previous target or backup record is unavailable, stop and
recover the recorded release/unit state manually; do not remove release data
or native Pi session files as part of bot recovery.

Rollback verifies the previous release's manifest before stopping the current
service or switching the target. The service restoration uses multiple steps;
only the active-release symlink replacement is atomic.

For an uncertain turn after a service restart, do not resend the prompt
automatically. Inspect `/status`, `/doctor`, and Pi's native session through
the normal terminal workflow before deciding whether to continue.
