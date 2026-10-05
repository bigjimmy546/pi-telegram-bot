status: approved
approved: 2026-10-05
public-amendment: 2026-10-05

# Telegram Pi bot

## 1. Goal / non-goals

Build a separate private Telegram front end for the installed Pi coding agent
in this source repository; credentials and runtime state remain external. It
must leave any existing bots unchanged.

The bot must:

- Run Pi as a full coding agent from the configured absolute Pi working
  directory, with Pi's normal file, shell,
  context-file, skill, extension,
  provider, and native-session behavior.
- Use the configured `paths.pi_cli` executable through its documented JSONL
  RPC mode. The supported version is `1.0.2`; implementation must
  record and test the supported version rather than copy Pi internals.
- Accept text, Telegram voice messages, photos, and documents. Voice uses
  the configured Groq Whisper credential and configuration.
- Deliver final text plus files and images that Pi explicitly asks the bot to
  send through validated `send_file` and `send_image` tools.
- Use Pi's native sessions in both directions: after its first accepted model
  turn materializes a Pi session file, a Telegram-created session can be
  resumed from the terminal, and terminal-created sessions under the configured
  absolute Pi working directory can be selected in Telegram.
- Discover models, supported thinking levels, commands, and skills from the
  live Pi runtime. The bot-owned model policy exposes every model returned by
  Pi `get_available_models` whose `provider == "ollama"`, plus only
  `antigravity/gemini-3.7-flash`. Models visible only in `ollama list` are not
  eligible until the operator adds their metadata to Pi's `models.json`. The bot
  labels non-`:cloud` Ollama models **Local**, `:cloud` models **Ollama
  Cloud**, and the Antigravity model **Remote**. New sessions inherit Pi's
  effective provider, model, and thinking defaults for the configured absolute
  Pi working directory from the effective Pi agent directory's `settings.json`
  plus an optional `.pi/settings.json` project override in the configured
  working directory. The bot refuses startup when those defaults are malformed or
  outside its allowlist; it never substitutes another model. Selecting the
  Antigravity model sets thinking to `high`.
- Let ordinary prompts use Pi's automatic skill routing. Support direct
  `/skill <name> <request>` invocation and a context-free `/skill` catalog.
- Preserve Pi's automatic compaction and provide manual `/compact`.
- Bundle related inputs, queue later work by default, allow explicit steering,
  show low-noise progress, and use reactions as the immediate lifecycle signal.
- Run continuously as a user systemd service, restart on failure, and start
  after reboot under the machine's existing user-service policy.

Non-goals for v1:

- No edits, shared package extraction, token reuse, state migration, or
  deployment changes in either existing Telegram bot.
- No `/project`; every Pi invocation uses the configured absolute Pi working
  directory.
- No `/clone`, `/tree`, or `/fork` controls. Native terminal Pi retains those
  capabilities.
- No automatic fallback between models or providers.
- No parallel Pi turns, worker pool, group chat, forum topic, webhook, public
  listener, Telegram Mini App, browser pairing, or scheduled task system.
- No bot-owned completed conversation transcript and no copying of native Pi
  history into SQLite.
- No skill installation, removal, editing, enable/disable controls, copied
  skill catalog, or pending tap-then-send skill state.
- No automatic Pi, dependency, application, or production update.
- No publication beyond the single approved sanitized public repository
  without a separate request.

## 2. Terms and state transitions

### Terms

- **Native Pi session**: a Pi JSONL session under the effective Pi agent
  directory's encoded working-directory session folder, owned by Pi and
  resumable from either Telegram or the terminal.
- **Pending session**: a bot-local empty session selected by `/new`, with a
  preallocated UUID, optional name, model, and thinking level. Pi 1.0.2 does
  not persist an empty session, so this state becomes a native Pi session only
  when its first accepted model turn creates the matching JSONL file.
- **Conversation binding**: the bot-local pointer from the configured user's
  authorized private chat to the selected pending or native Pi session.
- **Input bundle**: one or more authorized Telegram inputs collected before
  one Pi turn. Text-only bundles wait 5 seconds after the most recent item;
  bundles containing voice, photo, or document input wait 10 seconds.
- **Active turn**: one accepted Pi prompt with disposition `started` or
  `queued` whose RPC process has not yet emitted `agent_settled` or reached a
  terminal failure.
- **Queued bundle**: the next input bundle received while a turn is active. It
  starts after the active turn settles and its terminal output is recorded.
- **Steer**: an explicit request to deliver input to the active Pi turn using
  Pi's native steering behavior instead of the default next-turn queue.
- **Blocking UI request**: a Pi RPC extension-UI `select` or `confirm` request.
  RPC supplies no trustworthy extension identity, so the bot treats it as a
  generic exact-process request rather than attributing it to an extension.
- **Uncertain turn**: Pi accepted a prompt, but the bot lost authoritative
  completion evidence. It must never be retried automatically because the
  first attempt may have changed files or external state.
- **Output artifact**: a file or image explicitly queued by Pi for delivery to
  the authorized chat after validation.

### Session transitions

1. With no selected session, an ordinary prompt allocates a pending session under the configured absolute
   Pi working directory and dispatches its first turn.
2. `/new [name]` allocates and selects a blank pending session. `/clear` and
   `/reset` are accepted hidden aliases for `/new` without a name; none deletes
   or truncates the previous session. Its UUID, optional name, model, and
   thinking level are persisted in bot state until materialization.
3. `/sessions` shows all bot-local pending sessions followed by the 10 most
   recent native sessions under the configured absolute Pi working directory; `/sessions all [limit]` accepts
   native-session limits from 1 through 50. Empty pending sessions are labeled
   and not falsely advertised as terminal-resumable.
4. `/use <number|unique-id-prefix|full-id>` selects a pending or native session
   from the latest displayed list. Ambiguous or stale selections fail closed.
5. Model and thinking changes are session-scoped. They update bot state for a
   pending session and native Pi state for a materialized session. `/model`
   exposes only models allowed by the bot-owned policy. Switching sessions
   restores that session's settings and never changes Pi's global defaults.
6. Telegram and terminal use of one native session is sequential. The bot
   prevents two bot turns but does not claim it can lock an independently
   launched terminal Pi process.
7. `/compact [instructions]` runs native Pi compaction for the selected idle,
   materialized session and reports that an empty pending session has nothing
   to compact. It preserves the full stored transcript and changes only future
   model context. Automatic Pi compaction remains enabled.

### Input, queue, blocking UI, and reaction transitions

1. Authorization succeeds before reaction, download, transcription, state
   mutation, filesystem inspection, or Pi process creation.
2. An accepted input receives `👀` immediately on a best-effort basis.
3. A text item opens or extends a 5-second bundle. Voice, photo, or document
   input opens or converts it to a 10-second media bundle. `Send now` dispatches
   immediately.
4. While Pi is active, a new bundle queues for the next turn by default. An
   explicit `Steer current` action sends it through Pi's `steer` command.
5. Only one global Pi turn runs at a time. Normally the next queued bundle
   starts after the active turn settles. `/stop` first sends Pi `clear_queue`
   and waits for acknowledgement, discards any undelivered native steer or
   follow-up, then sends `abort`. Bot-local next-turn bundles are frozen until
   the operator explicitly sends or cancels them.
6. A blocking RPC UI `select` or `confirm` becomes Telegram buttons tied to the
   exact turn, process, and request. An unanswered request is cancelled after
   10 minutes. RPC UI `input` and `editor` requests are cancelled immediately;
   stale, duplicate, wrong-turn, and post-restart responses fail closed.
7. Successful completion replaces the source reaction with `👌`; terminal
   failure replaces it with `😨`. Reaction failure never changes the turn
   result.
8. One editable progress card reports bounded current activity. Tool-by-tool
   logs, chain-of-thought, and token-by-token text are not sent. The final
   answer is delivered separately and split only at Telegram-safe boundaries.

### Command surface

Visible commands:

| Command | Behavior |
| --- | --- |
| `/new [name]` | Allocate and select a blank pending Pi session. |
| `/sessions [all [limit]]` | List pending sessions and selectable native sessions under the configured working directory. |
| `/use <selector>` | Select a listed pending or native session. |
| `/model [selector]` | Show or change the selected session's live Pi model. |
| `/thinking [level]` | Show or change a level supported by that model. |
| `/skill` | Show a searchable or paginated live skill catalog without prompting Pi's model or creating a session entry. |
| `/skill <name> <request>` | Validate the live name and invoke Pi's native `/skill:<name> <request>` expansion in one turn. |
| `/compact [instructions]` | Compact the selected idle session through Pi. |
| `/status` | Show selected session, model, thinking, context, active/queued state, blocking UI state, and last delivery result. |
| `/stop` | Abort the active Pi turn without deleting its session or silently dispatching queued input. |
| `/usage` | Show Pi-reported session tokens, context usage, compactions, and cost when the provider reports it. |
| `/help` | Explain the compact command set, bundle delays, reactions, queueing, blocking UI, and sequential terminal sharing. |

Hidden but accepted commands are `/start`, `/clear`, `/reset`, and `/doctor`.
`/doctor` runs independent redacted checks for authorization/configuration,
Telegram, Pi executable/version, Pi RPC, Ollama/provider readiness, native
sessions, skill discovery, storage, Groq, ffmpeg, outbound artifacts, and
single-poller state. It does not mutate a session or call the model.

## 3. Data, integration and permission boundaries

### Local paths

| Purpose | Path | Rule |
| --- | --- | --- |
| Source | the source repository | Git-tracked source, tests, docs, and operations files; no secrets or runtime state. |
| Pi executable | configured `paths.pi_cli` (example: `/usr/bin/pi`) | Installed native runtime; never copied or patched by this project. |
| Pi config/auth/sessions | the effective Pi agent directory (default `~/.pi/agent`) | Shared unchanged with terminal Pi; sessions are selected, not copied. Credential values are never printed or stored by the bot. |
| Bot configuration | `~/.config/telegram-pi-bot/config.toml` | Non-secret settings, mode `0600`. |
| Bot secrets | `~/.config/telegram-pi-bot/secrets.env` | New BotFather token only, mode `0600`, created after SPEC approval and filled locally by the operator. |
| Bot state | `~/.local/state/telegram-pi-bot` | SQLite control state, pending bundles, attachments, artifact outbox, and redacted diagnostics. |
| Releases | `~/.local/opt/telegram-pi-bot/releases/<build-id>` | Immutable tested releases. |
| Active release | `~/.local/opt/telegram-pi-bot/current` | Atomic symlink selected only by guarded deployment. |
| User service | `~/.config/systemd/user/pi-telegram.service` | Exactly one long-polling process with bounded restart policy. |

- `paths.cwd`, `paths.pi_cli`, `paths.state_dir`, and `paths.groq_key_file`
  accept absolute paths or `~/` paths resolved against the service account's home.
- Pi's agent directory follows `PI_CODING_AGENT_DIR`, otherwise
  `~/.pi/agent`. Native session storage follows
  `PI_CODING_AGENT_SESSION_DIR`, then effective `sessionDir`, then Pi's
  documented encoded-working-directory default.

The repository must ignore secrets, local configuration, state, attachments,
logs, transcripts, caches, build output, and releases.

### Runtime and session boundary

- The implementation is Python and uses Pi as a subprocess through documented
  RPC commands/events. It does not import private Pi internals or emulate the
  model loop.
- Every runtime process has explicit `cwd` set to the configured absolute Pi
  working directory, uses Pi's normal user configuration and resource
  discovery, and trusts that fixed project for this invocation. It does not
  disable the service account's installed skills or extensions.
- RPC stdout is protocol-only and drained continuously as bytes split strictly
  on LF. Stderr is diagnostic-only, bounded, and redacted.
- Active turn processes are short-lived: start a pending session by passing
  its stored `--session-id`, `--name`, `--provider`, `--model`, and `--thinking`
  values, or resume one native session; then serve its
  prompt/steer/blocking-UI/abort lifecycle through `agent_settled` when a run
  starts. Metadata-only inspection must not add messages to a session.
- A successful `prompt` response is interpreted by disposition. `started`
  waits for `agent_settled`; `queued` also waits under the six-hour turn
  deadline; `handled` means an extension consumed the input, so the bot does
  not wait for `agent_settled` and instead drains records already emitted for
  that command, queries `get_last_assistant_text`, and completes explicitly
  with new text or a handled-without-text notice. It must not reuse unchanged
  assistant text from before the handled command. An unknown successful
  disposition is a protocol error after acceptance and is never auto-retried.
- After the first `started` or `queued` prompt, the bot recognizes a pending
  session as materialized only after finding and validating the matching Pi
  v3 session header. It then atomically replaces that pending record with the
  native binding; until then the pending record remains authoritative.
- Session discovery reads only the documented native session header and the
  minimum documented metadata required for display. It never copies message or
  tool content into bot state.
- The bot's SQLite state may retain pending/queued input until terminal
  dispatch or cancellation so restarts do not lose work. Completed prompt and
  response bodies are removed; native Pi JSONL remains the conversation record.

### Model and skill boundary

- `/model` uses Pi's live `get_available_models`, then admits every
  `provider == "ollama"` model and only
  `antigravity/gemini-3.7-flash`. All other providers and Antigravity models
  are hidden even if Pi exposes them.
- Non-`:cloud` Ollama models are labeled **Local**; `:cloud` Ollama models are
  labeled **Ollama Cloud**; `antigravity/gemini-3.7-flash` is labeled
  **Remote**. Choosing either remote class is the explicit data-egress
  decision for that session. There is no automatic fallback.
- New sessions inherit Pi's effective `defaultProvider`, `defaultModel`, and
  `defaultThinkingLevel` for the configured absolute Pi working directory.
  Project settings override user
  settings exactly as Pi documents; an absent thinking setting means Pi's
  documented `medium` default. Changes take effect for new Telegram sessions
  after the service restarts. Existing pending and native sessions keep their
  stored session-specific settings.
- The inherited model must be admitted by the bot policy and present in Pi's
  live catalog. If the inherited model is
  `antigravity/gemini-3.7-flash`, its inherited thinking level must be `high`.
  Invalid, unavailable, or disallowed defaults fail closed without fallback.
  Selecting `antigravity/gemini-3.7-flash` in `/model` also atomically sets
  thinking to `high`. `/thinking` subsequently exposes only the live levels
  supported by the selected model.
- `/skill` uses Pi's live `get_commands` response filtered to native skill
  commands. Catalog paging is Telegram-only and creates no Pi prompt or native
  session entry.
- Direct `/skill <name> <request>` requires one exact enabled live name, then
  sends Pi `/skill:<name> <request>`. Pi performs native input expansion and
  loads full instructions only for the invocation. Ordinary prompts retain
  Pi's normal automatic skill selection.

### Permission and trust boundary

- Exactly the configured positive Telegram user ID in a private chat is
  authorized. Every
  other user/chat fails before any side effect, including reaction and file
  download.
- Pi runs with the service account's operating-system permissions and is not
  sandboxed. The
  Telegram identity allowlist is the remote-access boundary.
- The service account's installed permission-gate extension remains active.
  Its current
  source matches only `rm -r`, `rm -rf`, or `rm --recursive`; any command
  containing `sudo`; and `chmod` or `chown` followed later by `777`. The bot
  does not claim broader protection.
- Because RPC UI records contain no extension identity, the bot generically
  relays every blocking `select` and `confirm` request and cancels unsupported
  blocking `input` and `editor` requests. A Telegram response applies only to
  the exact pending request in the exact active process and is never reusable.
- Bot code must not weaken the permission extension, alter Pi configuration,
  or claim that project trust restricts tool filesystem access.
- One positive Telegram user ID is configured locally. Authorization still occurs
  before update claims, reactions, downloads, replies, state changes, or Pi calls.
- The bot is Linux/systemd-user software and runs Pi unsandboxed with the service
  account's normal file and shell permissions.
- Public verification records test categories and reproducible commands, not one
  operator's private paths, IDs, bot handle, session IDs, timestamps, or live results.

### Telegram, media, artifact, and network boundary

- Telegram uses long polling with a new dedicated bot token. No existing bot
  token or poller is reused.
- Voice is downloaded only after authorization, converted with `/usr/bin/ffmpeg`
  when required, and transcribed using
  the configured `paths.groq_key_file` (mode `0600`, default
  `~/.config/groq/api_key`), base URL
  `https://api.groq.com/openai/v1/`, and model
  `whisper-large-v3-turbo`.
- One inbound bundle accepts at most 10 media items and 50 MiB total. Each
  voice/document item is capped at 20 MiB and each photo at 10 MiB. The bot
  rejects an oversized item before downloading its body when Telegram metadata
  supplies the size, and otherwise stops at the cap while streaming.
- Photos are normalized to a Pi-supported image payload. If the selected model
  cannot accept images, dispatch fails before starting a turn and asks the
  operator to change models.
- Documents are stored under the bot attachment directory with generated names
  and restrictive modes, then referenced by an explicit local path in the Pi
  request. Telegram filenames never become trusted paths.
- A bot-owned Pi extension, loaded explicitly with `--extension`, registers
  `send_file` and `send_image`. Each tool call uses an authenticated private
  Unix-domain JSONL socket to synchronously ask the Python parent to validate
  and durably queue the artifact. The extension returns the parent's real
  accepted/rejected result to the model; it never writes custom records to
  RPC stdout and never calls Telegram itself.
- The private socket lives under bot state with mode `0600`, uses a fresh
  per-process capability, has bounded framing and timeouts, and accepts one
  child connection. The parent copies and hashes accepted bytes before it
  replies success. Socket failure rejects the tool call.
- One turn may queue at most 5 outbound artifacts and 50 MiB total.
  `send_image` accepts PNG, JPEG, or WebP up to 10 MiB each. `send_file`
  accepts up to 20 MiB each and only PDF; `text/*` source/document formats;
  JSON, JSONL, YAML, or XML; Office Open XML (`.docx`, `.xlsx`, `.pptx`); or
  ZIP, TAR, and GZIP archives. Executables and unknown binary types are
  rejected.
- Outbound paths must resolve to regular files beneath the configured absolute
  Pi working directory. Symlinks
  are resolved before validation; after opening, the actual file descriptor's
  path and regular-file type are revalidated before any bytes are copied. The
  staged copy's hash is checked again at delivery; only the authorized chat
  can receive it. File bytes do not enter model context merely because they
  are being delivered.
- The resolved path relative to the configured absolute Pi working directory
  is rejected when any component
  starts with `.` or when a case-insensitive regex search of any component
  matches
  `secret|credential|token|passw|api[_-]?key|\.pem$|\.key$|^id_(rsa|ed25519|ecdsa)`.
  For the final basename only, a leading `tokenizer` does not count as a
  `token` match; any other suspicious match still rejects it. These rules
  intentionally reject legitimate hidden files such as `.github/*.yml`; the
  operator must copy one to a non-hidden, non-sensitive path before sending it. The
  rules protect broad secret-bearing path classes but are not represented as
  content-aware secret detection.
- Completed/cancelled inbound staging and delivered artifact copies are
  deleted immediately on a best-effort basis, with a sweeper deleting orphaned
  staging older than 24 hours. Pending/queued input expires after 24 hours.
  Failed outbound delivery remains retryable for 24 hours, then its staged copy
  expires; the original project file is never deleted. Redacted diagnostic and
  delivery metadata is retained for 30 days. Completed prompt and response
  bodies have no bot-owned retention.
- Bot-owned HTTP clients connect only to Telegram and Groq; deliberate
  dependency/source work may also use its configured registries. Pi itself is
  unsandboxed and retains the same arbitrary network access it has in a normal
  terminal through model providers, shell commands, and tools. This project
  does not claim to enforce Pi network egress.

### Secrets and logging

- `secrets.env` is created with mode `0600`; the operator enters the token
  locally so it never appears in chat or shell history. Startup refuses an empty token or
  unsafe permissions.
- No token, credential, authorization header, prompt body, transcript, file
  content, raw Telegram update, or unredacted provider error is logged.
- Diagnostics report bounded categories, identifiers, counts, and corrective
  actions. HTTP logging must never expose Telegram token-bearing URLs.

## 4. Modules and seams

### Core module: Pi runtime

This module is the most expensive interface to get wrong because Pi process
lifecycle, RPC framing, session ownership, event ordering, blocking UI, and
uncertain outcomes would otherwise spread across Telegram handlers and queue
logic.

#### Chosen interface: turn-oriented runtime

```python
class PiRuntime(Protocol):
    async def inspect(
        self, session: NativeSessionRef | None
    ) -> RuntimeSnapshot: ...
    async def list_sessions(self, limit: int) -> list[NativeSession]: ...
    async def configure_session(
        self, session: NativeSessionRef, changes: SessionConfigChange
    ) -> RuntimeSnapshot: ...
    async def start_turn(
        self, request: TurnRequest, events: RuntimeEventSink
    ) -> ActiveTurn: ...
    async def compact(
        self, session: NativeSessionRef, instructions: str | None
    ) -> CompactionResult: ...

class ActiveTurn(Protocol):
    async def steer(self, content: TurnContent) -> None: ...
    async def answer_ui(self, request_id: str, response: UiResponse) -> None: ...
    async def abort(self) -> None: ...
    async def wait(self) -> TurnResult: ...
    async def close(self) -> None: ...
```

The interface includes these invariants and error modes:

- `start_turn` returns only after Pi has accepted a known disposition safely
  enough for the caller to own an `ActiveTurn`; the returned handle may already
  be terminal for `handled`, while prompt acceptance is separately reported in
  events.
- The coordinator, not `PiRuntime`, allocates pending-session UUID/name/config
  state because Pi cannot persist it before the first prompt. `start_turn`
  supplies all stored launch flags on that first turn without changing Pi's
  global configuration, then includes any validated native materialization in
  `TurnResult` for the coordinator to commit atomically.
- `configure_session` validates a partial name/model/thinking change against
  the live bot-owned model policy and applies it to a native session in a
  bounded order. Pending-session configuration is changed transactionally by
  the coordinator. Selecting the Antigravity profile includes thinking `high`
  in the same operation.
- `RuntimeSnapshot` contains live models, thinking levels, commands/skills,
  session stats, and provider readiness without credential values.
- `TurnResult` distinguishes rejected-before-acceptance, completed, handled,
  aborted, failed-after-acceptance, and uncertain. Only
  rejected-before-acceptance may be retried automatically; `handled` is a
  terminal accepted result and may explicitly contain no assistant text.
- Events are ordered per process and normalized to progress, assistant text,
  tool activity summary, blocking UI request, artifact result, warning, and
  settlement. Raw RPC dictionaries never cross this interface.
- `answer_ui` supports only the exact live `select` or `confirm` request;
  unsupported dialog types and expired requests are cancelled.
- `abort` owns the native sequence: acknowledged `clear_queue`, discard native
  steering/follow-up, then `abort`. If clearing cannot be confirmed, it
  terminates only its child process, freezes bot-local queued input, and marks
  the result uncertain rather than risk continuing native queued work.
- `close` is idempotent and owns bounded graceful shutdown followed by
  termination of only the child process it created.

**Hidden:** Pi CLI arguments, binary JSONL framing, request correlation,
continuous pipe draining, stderr handling, RPC event variants, metadata
processes, pending-session launch flags, native name/model/thinking command
sequences, session-header parsing, the bot-owned artifact extension and
private Unix socket, process signals, cleanup, and redaction.

**Seam:** `PiRuntime` lives between the coordinator and
`src/telegram_pi_bot/pi_runtime.py`. Production uses the installed Pi adapter;
tests use one deterministic fake through the same interface. What varies is
the Pi process/event source, not Telegram behavior.

**Deletion prediction:** deleting this module would force RPC commands,
process cleanup, session configuration, event/UI correlation, artifact IPC,
and uncertainty rules into the coordinator, doctor, commands, and tests. Its
complexity would spread rather than disappear.

#### Rejected interface: raw RPC client

```python
class PiRpcClient(Protocol):
    async def start(self, arguments: list[str]) -> None: ...
    async def send(self, record: dict) -> dict: ...
    def events(self) -> AsyncIterator[dict]: ...
    async def stop(self) -> None: ...
```

Rejected because it exposes Pi protocol records and lifecycle ordering to every
caller, leaving a shallow wrapper and duplicating failure logic.

### Core module: conversation coordinator

This module owns the state transitions that are easiest to race or lose across
restarts: bundling, one-turn leasing, queued work, steering, blocking UI,
reactions, progress, cancellation, and terminal delivery.

#### Chosen interface: state machine plus effects

```python
def transition(state: BotState, action: Action) -> Transition:
    """Return the next state, ordered effects, and user-visible replies."""
```

The interface includes these invariants and error modes:

- An action is already authorized or is an explicit unauthorized action; the
  transition decides no external side effect itself.
- State and effects are committed in an order that makes redelivery
  idempotent after restart.
- At most one global active-turn lease and one blocking-UI owner exist.
- Every Telegram callback contains a bounded opaque key and is revalidated
  against current state before it can mutate anything.
- A transition cannot turn an uncertain Pi result into success or retry it.

**Hidden:** bundle timers, lease tokens, callback generations, queue ordering,
reaction targets, progress throttling, blocking-UI expiry, retryable outbound
records, and restart recovery rules.

**Seam:** the pure transition interface lives between Telegram/Pi/timer/storage
adapters and `src/telegram_pi_bot/coordinator.py`. The effect runner is the only
caller allowed to perform network, process, filesystem, or clock effects.

**Deletion prediction:** deleting the coordinator would distribute race
conditions and restart behavior across Telegram callbacks, timers, SQLite,
and Pi event handlers. Its complexity would spread across all callers.

#### Rejected interface: direct side-effecting handlers

```python
async def handle_telegram_message(update, telegram, pi, store) -> None: ...
```

Rejected because authorization ordering, bundles, active-turn state,
blocking UI, restart recovery, and delivery idempotency could not be verified
through one stable interface.

## 5. Edge and failure cases

- Unauthorized, non-private, malformed, duplicate, and stale Telegram updates
  have no filesystem, network-download, Pi, Groq, session, or state effect.
- Edited messages are stale updates. They do not replace pending input or
  dispatch another prompt, command, or attachment.
- A duplicate Telegram update or callback is idempotent and cannot start a
  second Pi turn, deliver a second artifact, or answer a blocking UI request twice.
- A service restart preserves the selected session, non-terminal bundle,
  queue, and outbound-delivery records. It cancels any orphaned blocking UI and
  marks an accepted but unterminated Pi turn uncertain.
- If Pi or the selected Ollama/Antigravity model is unavailable before prompt
  acceptance, the turn fails with one actionable error and no fallback. If
  failure occurs after acceptance, the result is failed or uncertain and is
  never auto-retried.
- Malformed RPC output, correlation mismatch, unexpected EOF, pipe
  backpressure, hung shutdown, and child signal failure are bounded and cannot
  target an unrelated terminal Pi process.
- Provider/model catalog refresh failure leaves the prior displayed menu stale
  and non-actionable; it never silently selects another model.
- Image input to a text-only model, unsupported document input, failed Groq
  transcription, ffmpeg failure, expired attachment, or unsafe filename fails
  before Pi prompt acceptance and keeps retry/cancel ownership explicit.
- A queued bundle received during an active turn starts only once. `/stop`
  confirms `clear_queue` before `abort`, cancels undelivered native steering,
  and never silently launches the bot-local next-turn bundle. Stale `Send`,
  `Cancel`, or `Steer` buttons fail closed.
- Blocking UI timeout, service restart, process exit, mismatched request ID, or
  Telegram delivery failure cancels the request. UI button delivery is retried
  only while the exact request remains live; `input` and `editor` are never
  presented as supported Telegram interactions.
- `/compact` is refused during an active turn. A compaction failure leaves the
  prior active branch usable and does not claim token reduction.
- Simultaneous terminal use is an operating-rule violation, not a lock the bot
  can enforce. A post-turn native-session sanity check may warn about a likely
  external modification, but detection is best-effort and its absence never
  proves exclusive access.
- Telegram reaction or progress-card failure does not cancel accepted Pi work.
  Final delivery is durable and retryable without re-running Pi. If Telegram
  accepts a send but its acknowledgement is lost, retrying may duplicate a
  message or artifact; exactly-once Telegram delivery is not guaranteed.
- `prompt` dispositions `started`, `queued`, and `handled` each terminate by
  their defined path; the six-hour overall turn deadline prevents a wait for
  an event Pi does not promise from hanging the coordinator forever.
- Telegram text limits cause bounded, ordered splitting. Empty assistant output
  is reported explicitly; tool success alone is not fabricated as a textual
  answer.
- Inbound bundles and output artifacts enforce their stated item, aggregate,
  type, and retention limits. Output rejects directories, devices, sockets,
  escaping symlinks, changed bytes, unsupported types, excessive size, missing
  files, unavailable IPC, and paths outside the allowed roots. Failure to
  deliver a staged copy never deletes the project source file. Known
  hidden path components and secret-like component names are denied after path
  resolution even when their file type would otherwise be allowed.
- SQLite corruption, unsafe state/config permissions, empty secrets, duplicate
  pollers, and unwritable storage make readiness fail before polling starts.

## 6. Acceptance criteria

### A. Frozen local gate

From the repository root, all commands pass:

```sh
uv lock --check
uv sync --frozen
uv run python -m compileall -q src tests
uv run python -m unittest discover -s tests -v
git diff --check
```

Tests reach the two core modules only through their documented interfaces and
prove at least:

1. authorization precedes every reaction, download, state mutation, Groq call,
   Pi process, blocking UI response, and artifact delivery;
2. 5/10-second bundling, `Send now`, one global lease, default next-turn queue,
   explicit steer, `/stop` clear-queue-before-abort ordering, restart recovery,
   and stale callbacks;
3. `👀 -> 👌/😨` lifecycle using Telegram-supported reaction values and
   non-fatal reaction failure;
4. pending-session allocate/name/list/select behavior, first-turn launch flags
   and native materialization, terminal resume only after materialization,
   non-destructive aliases, per-session model/thinking restoration, and
   best-effort external modification warnings without a false locking claim;
5. live-catalog parsing admits all and only Pi-advertised models whose provider
   is `ollama`, plus `antigravity/gemini-3.7-flash`; new sessions inherit Pi's
   effective provider/model/thinking defaults for the configured absolute Pi
   working directory, including project-over-user precedence and the documented absent-thinking default;
   disallowed, unavailable, or malformed inherited defaults fail closed; the
   AGY profile requires `high`; local/remote labels remain accurate; and no
   fallback is automatic;
6. `/skill` catalog inspection creates no Pi message/session entry, while
   `/skill example <request>` becomes exactly one validated native
   `/skill:example <request>` prompt;
7. manual/automatic compaction rules and refusal during an active turn;
8. generic `select`/`confirm` exactness, timeout/restart cancellation,
   immediate `input`/`editor` cancellation, and no reusable grant;
9. all `started`, `queued`, and `handled` prompt dispositions, prompt rejection
   versus accepted failure/uncertainty, handled-without-text completion, bounded
   waits, and zero automatic retry after acceptance;
10. concrete inbound item/aggregate caps, exact Groq/ffmpeg configuration,
    artifact-extension Unix-socket authentication, synchronous accepted/rejected
    results, output count/path/symlink/type/size/hash/hidden-and-secret-component
    validation, 24-hour staging expiry, 30-day metadata retention, and durable
    delivery without prompt replay;
11. completed prompt/answer bodies are absent from bot SQLite while native Pi
    session content remains Pi-owned;
12. `/doctor` checks are independent, structured, non-mutating, and redact all
    configured sentinel secrets.

### B. Installed Pi compatibility gate

These checks use the installed Pi runtime without a Telegram token:

1. A metadata-only RPC process returns state, models, thinking levels, and
   commands/skills without creating a user/assistant session entry.
2. A tool-disabled ephemeral prompt to the selected local default reaches
   Ollama and emits ordered acceptance, assistant output, and `agent_settled`.
3. A pending session is absent from Pi storage before its first prompt; its
   first model turn passes the stored ID/name/model/thinking launch flags,
   materializes a matching native session, and can then be resumed by
   the configured Pi executable's `--session <id>` inspection. A terminal-created temporary
   session is discoverable through `list_sessions`.
4. RPC extension-UI fixtures prove generic `select`/`confirm` correlation and
   `input`/`editor` cancellation without executing a dangerous command.
5. A loaded bot-owned artifact extension proves an accepted and a rejected
   synchronous Unix-socket tool result without contacting Telegram.
6. Compatibility checks record the configured Pi executable's `--version` and fail clearly on an
   unsupported protocol instead of guessing.

### C. Service and release gate

1. A build from a clean committed tree installs an immutable release and never
   activates it merely for syntax checking.
2. `secrets.env` and `config.toml` exist outside the repository with mode
   `0600`; startup rejects missing values or weaker permissions without
   printing the token.
3. `systemd-analyze --user verify` accepts `pi-telegram.service`; the unit uses
   the active-release symlink, the service account's home working directory,
   bounded restart behavior, an `[Install]` section with
   `WantedBy=default.target`, and no token on its command line.
4. A duplicate-poller guard prevents two processes using the new token.
5. Deployment runs `systemctl --user enable --now pi-telegram.service` so the
   bot starts under the existing lingering user manager after reboot.
   Deployment and rollback preserve the previous exact unit, enablement state,
   and active-release target. Rollback never touches Pi native sessions.

### D. Real private-chat gate

After the operator enters the new BotFather token locally:

1. An authorized text turn shows `👀`, one progress card, a final answer,
   and `👌`; a controlled failure ends with `😨`.
2. Voice, photo, and document inputs each complete through the intended route;
   a multi-item media bundle sends once and `Send now` bypasses its timer.
3. A second input queues during a live turn, explicit steer reaches the current
   turn, and only one local model job runs.
4. `/sessions` shows a new empty Telegram session as pending rather than native;
   after its first turn it can select that materialized session and one
   terminal-created native session, and each remains resumable from the other
   surface when idle.
5. `/model` shows every Pi-advertised `ollama` model plus only AGY 3.7 Flash
   and excludes models visible only to `ollama list`; a new Telegram session
   matches Pi's effective provider/model/thinking defaults for the configured
   working directory,
   AGY defaults and selections use `high`, `/thinking` shows live valid levels,
   and an unavailable or disallowed default does not trigger fallback.
6. `/skill` lists the live catalog without creating a Pi message; direct
   `/skill example <request>` invokes the native skill in the same turn.
7. `/compact`, `/usage`, `/status`, hidden `/doctor`, `/stop`, generic blocking
   UI buttons, `send_file`, and `send_image` each pass one bounded live check.
8. A non-allowlisted test update is rejected before download or Pi invocation.
9. Service restart and reboot restore polling, selected idle session, and safe
   queued/outbound state without duplicate execution.

### E. Independent security and deployment review

An independent reviewer must check security boundaries and deployment changes
before activation. The review includes this SPEC, the exact diff, and the
acceptance commands. The reviewer reruns checks read-only and reports blocking
findings before deployment proceeds.

## 7. Decisions taken

1. **Separate third bot and token.** Rejected reusing an existing bot/token
   because it would risk duplicate polling and couple rollback paths.
2. **Full coding agent.** Rejected chat-only mode because the goal is parity
   with terminal Pi.
3. **Python plus Pi RPC.** Rejected a TypeScript Pi-SDK rebuild because the
   Python implementation provides the required Telegram/media/state shape,
   while RPC exposes Pi's native lifecycle without modifying existing code.
4. **Separate project folder.** Rejected modifying or extracting a shared
   package from either live bot; v1 prioritizes isolation and rollback.
5. **Configured working directory.** Rejected `/project` selection because
   the operator always uses Pi from home and does not want the extra control;
   the absolute Pi working directory is configured once, locally.
6. **Native sessions as the record.** Rejected bot-owned transcripts and prompt
   replay because they would diverge from terminal Pi.
7. **One global turn.** Rejected concurrent sessions because they would compete
   for the local model and complicate selection and queue ownership.
8. **Queue by default with explicit steer.** Rejected implicit steering because
   later messages should not silently change work already underway.
9. **Full media input and explicit artifact output.** Rejected text-only mode
   because the operator wants parity with the existing bot workflow.
10. **Lifecycle reactions and one progress card.** Rejected a permanent `👍`
    and verbose streaming because `👀 -> 👌/😨` communicates accepted versus
    terminal state with less noise.
11. **Automatic skill routing plus two explicit controls.** Rejected a
    mandatory pick-then-send flow. Direct invocation is one message and
    `/skill` is a context-free memory aid.
12. **Manual and automatic compaction; no clone.** Rejected `/clone` in v1
    because it preserves rather than reduces context and is not needed for the
    primary mobile workflow.
13. **Bot-owned allowlist; Pi-owned startup defaults.** `/model` admits every
    model advertised by Pi with `provider == "ollama"`, plus only
    `antigravity/gemini-3.7-flash`; all other live providers/models and models
    visible only to `ollama list` are hidden. New sessions inherit Pi's
    effective provider/model/thinking defaults for the configured absolute Pi
    working directory, so terminal
    Pi and Telegram share one startup-default source. The bot validates those
    defaults against its allowlist and live catalog, requires `high` for an AGY
    default, labels remote models, and never falls back automatically.
14. **Per-session model/thinking state.** Rejected global changes because
    switching one Telegram session must not alter another or Pi's defaults.
15. **Generic blocking RPC UI in Telegram.** Rejected attributing requests to
    an extension because RPC carries no identity. Exact `select` and `confirm`
    requests are time-bounded; `input` and `editor` are cancelled. The
    installed gate's three regex families are documented without claiming
    broader protection.
16. **Hidden `/doctor`.** Rejected removing remote diagnostics, but omitted it
    from the main menu because it is an exceptional troubleshooting control.
17. **Turn-oriented Pi runtime with deep configuration operations.** Rejected a
    raw RPC interface because process, pending-session materialization,
    name/model/thinking, UI,
    artifact-IPC, and protocol complexity would leak into callers.
18. **Coordinator state machine.** Rejected direct side-effecting handlers
    because restart, queue, and blocking-UI races need one testable interface.
19. **External mode-`0600` secret environment.** Rejected a repository `.env`
    and chat delivery of the token because either could leak the BotFather
    credential.
20. **Long-polling user service.** Rejected webhook/public infrastructure and a
    manually launched process because this is a private always-on workstation
    bot.
21. **Bot-owned artifact extension with private parent IPC.** Rejected an
    impossible Python registration through Pi RPC and rejected custom stdout
    records. The loaded extension waits for the Python parent's durable
    validation result over an authenticated Unix socket.
22. **Clear Pi's native queue before abort.** Rejected plain `abort` because Pi
    may otherwise continue native steering or follow-up messages. Bot-local
    next-turn work remains frozen and recoverable.
23. **Enforce only bot-owned network policy.** Rejected a claim that this
    unsandboxed project limits Pi's shell/tool egress. Only the bot's HTTP
    clients are constrained to Telegram and Groq.
24. **Sequential terminal sharing is an operating rule.** Rejected a native
    lock or reliable conflict-detection claim. Post-turn checks may warn only
    on a best-effort basis.
25. **Concrete transfer and retention limits.** Rejected unspecified Telegram
    limits and “bounded retention”; item/count/aggregate caps, allowed outbound
    types, and 24-hour/30-day cleanup periods are part of the contract.
26. **Bot-local empty sessions.** Rejected claiming `/new` immediately creates
    a native Pi file: Pi 1.0.2 persists no session until a prompt. The bot owns
    the pending UUID/name/model/thinking and supplies them on the first turn.
27. **Explicit prompt dispositions.** Rejected waiting unconditionally for
    `agent_settled`; `handled` is terminal without a run, while `started` and
    `queued` wait under the turn deadline.
28. **Broad outbound path denylist.** Rejected an enumerated list of known
    secret files because new tools create new credential locations. Every
    resolved hidden component and every secret-like component name fails
    closed, with a narrow `tokenizer*` basename exception and no claim of
    content-aware DLP.
