# Security

This bot launches Pi with the service account's normal, unsandboxed filesystem
and shell permissions. Run it only with a private BotFather token and one
explicitly configured Telegram user ID.

Version 0.1.0 is unsupported because it passes `TELEGRAM_BOT_TOKEN` into Pi
child environments and treats edited messages as new prompts. Fixes for both
defects are included in version 0.1.1.

When this repository is public, report vulnerabilities from its Security tab
using "Report a vulnerability". Reports are private. The maintainer must enable
GitHub private vulnerability reporting immediately after making the repository
public and verify that the reporting button is available before announcing it.
GitHub does not provide this reporting form while the repository is private.
Do not open a public issue containing tokens, credentials, session files,
prompts, local paths, or exploit details.

The bot removes `TELEGRAM_BOT_TOKEN` from Pi's child environment, including
explicit environment overrides. This reduces accidental exposure through tool
environment dumps. Pi remains unsandboxed and can read files accessible to the
service account, including the secrets file; this is not credential isolation.

Only Pi 1.0.2 is supported, as enforced by the doctor's version check.

Telegram delivery is retryable, not exactly-once. If Telegram accepts a send
but its acknowledgement is lost, a retry can duplicate the text or artifact.
The bot never reruns the Pi prompt to retry delivery.
