# Acceptance evidence

This ledger separates automated contracts, installed-Pi compatibility,
service/release behavior, and real private-chat behavior. Mark a row complete
only when its listed evidence was directly observed.

## Automated and local evidence

- [ ] Frozen gate (`uv lock --check`, `uv sync --frozen`, `compileall`, full `unittest`, `diff check`).
- [ ] SPEC A1, A2, A3: authorization ordering, bundle/lease/queue/stop/recovery, and lifecycle reactions.
- [ ] SPEC A4, A5, A6: pending/native sessions, model policy, skill catalog and direct skill invocation.
- [ ] SPEC A7, A8, A9: compaction, blocking UI, prompt dispositions and uncertainty/no-retry behavior.
- [ ] SPEC A10, A11, A12: media/artifact constraints, transcript ownership, and redacted independent doctor checks.
- [ ] Operations tests prove clean/exact release guards, frozen build, non-activation, secret/config modes, lingering prerequisite, previous-state backup and restoration, atomic target switch, duplicate-poller refusal, enablement on deploy, and rollback isolation from `.pi`.
- [ ] `ops/verify-unit.sh` accepts unit syntax against the local launcher without changing `current`.

## Installed Pi compatibility evidence

- [ ] SPEC B1, B6: metadata-only RPC on supported Pi version reports models, thinking levels, and commands with session storage unchanged, no prompt, and no Telegram contact.
- [ ] SPEC B2: tool-disabled prompt returns `PI_OK` and `agent_settled` without contacting Telegram.
- [ ] SPEC B3: pending-session flags/materialization and terminal/native session discovery pass the installed runtime probe.
- [ ] SPEC B4: generic blocking UI correlation passes against installed Pi.
- [ ] SPEC B5: loaded bot-owned artifact extension proves accepted and rejected synchronous Unix socket tool results without contacting Telegram.

## Service and release evidence

- [ ] SPEC C1: clean committed build creates a checksummed immutable release and leaves `current` unchanged.
- [ ] SPEC C2: external config and `secrets.env` have mode `0600`; startup rejects missing/unsafe values without revealing token contents.
- [ ] SPEC C3: user unit verification accepts active-release path, home-relative working directory, bounded restart behavior, install target, and no token.
- [ ] SPEC C4, C5: duplicate poller is refused; deploy enables and starts the service under existing lingering; failed activation and rollback restore the exact prior target, unit, and enablement state without touching Pi sessions.

## Real private-chat evidence

- [ ] Authorized text turn shows immediate reaction, bounded progress, final answer, and successful terminal reaction; controlled failure ends in the failure reaction.
- [ ] Voice, photo, document, multi-item media bundle, and `Send now` pass.
- [ ] Queue, explicit steer, stop ordering, and one active local model job pass.
- [ ] Telegram and terminal can resume the same materialized native session sequentially; empty sessions remain labeled pending.
- [ ] Model allowlist/default, thinking levels, skill catalog/direct invocation, compaction, usage, status, doctor, blocking UI, `send_file`, and `send_image` pass their bounded live checks.
- [ ] A non-allowlisted synthetic update is rejected before download or Pi invocation.
- [ ] Service restart and reboot restore safe polling/session/queue state without duplicate execution.

No automated or local runtime result proves Telegram delivery. Record date, commit/release ID, exact check, and observed result beside a row when completed.
