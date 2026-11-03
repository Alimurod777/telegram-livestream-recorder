# Current architecture and rules for all AI assistants

Updated: 2026-11-03. This file is the current reference. Historical descriptions
in memory.md may describe earlier implementations; the rules below take priority
when they conflict with old architecture notes.

## User requirement: multiple recordings per owner

A bot user is the person chatting with the bot (`owner_user_id`). A Telegram
account is an authenticated Telegram identity (`client.me.id`). These are
separate concepts: the owner can add multiple accounts via `/addaccount`.

Example:

- Owner 7 logs in with Telegram account A and adds account B.
- `/record @chat_one` starts recording chat one with account A.
- `/record @chat_two` starts recording chat two with free account B.
- Both run at once and deliver their own recordings to owner 7.
- A third recording requires a third available account. Existing recordings
  continue unchanged when there is no free account.
- Two session strings for the same Telegram ID still count as ONE account.

Do not add an `is_recording(owner)` early return to `/record` or start logic.
Only deduplicate the same `(owner, chat)`. Enforce account capacity across ALL
owners using Telegram identity, including during failover and concurrent starts.

## Code map and data identities

- `bot.py`: manual urllib Bot API polling, login, commands, account selection,
  optional subscription checks, delivery, progress and diagnostic lifecycle.
- `stream_recorder.py`: exports the one `stream_recorder` singleton. Import it
  from here; do not construct another recorder in application code.
- `livestream/recorder.py`: `StreamRecorder`, `RecordingSession`, account capacity,
  per-recording start/stop, watchdog, duration limit and recovery.
- `livestream/accounts.py`: owned pool, health, atomic account reservation,
  FloodWait and busy counters. Every extra account selection is owner-scoped.
- `livestream/event_handler.py`: frames and call events, routed by BOTH
  PyTgCalls client and chat. It receives a recording key, not necessarily a user ID.
- `livestream/audio.py`, `muxer.py`, `source_manager.py`, `priority_manager.py`:
  native frames, mixing, encoding and rolling media parts.
- `livestream/uploader.py`, `mtproto_uploader.py`: manifests, delivery windows,
  per-session upload state, user/bot MTProto and Bot API fallback.
- `livestream/diagnostics.py`: async bounded Telegram diagnostics queue.
- `livestream/subscription.py`: optional Bot API membership gate.
- `database.py`: SQLite recording history, owned pool and waiting targets;
  primary session persistence also supports MongoDB and local backups.
- `config.py` loads `config.example.py`; settings come from `.env`.
- `docs/FOYDALANISH.md`: end-user command guide in Uzbek.
- `docs/ALWAYSDATA.md`, `scripts/start_alwaysdata.sh bot|check`,
  `deploy/alwaysdata/run_service.sh`: Alwaysdata setup
  and foreground launcher. Use Advanced > Services, one process per token.
  Set panel Working directory empty (or relative `telegram-livestream-recorder`),
  never `/home/account/...` in that field. Keep the Command script path absolute.
  Include `$HOME/bin` and `$HOME/.local/bin` on PATH for account-installed FFmpeg.
  Alwaysdata restarts send SIGHUP; preserve its graceful shutdown handler along
  with SIGTERM/SIGINT. The launcher must exec Python and set the repository cwd.
  Documentation/service scripts do not register or restart panel services by themselves.

`StreamRecorder.active_sessions` keys are `(owner_user_id, channel_id)` tuples.
Legacy integer keys are read only for compatibility with older test fixtures.
Use these methods instead of assuming the mapping is keyed only by owner:

- `get_sessions(owner)` returns all of that owner's active recordings.
- `get_session(owner, channel)` selects one exact recording.
- `get_session((owner, channel))` selects the same exact recording.
- `get_session(owner)` returns a recording only if exactly one is active;
  it returns None when multiple recordings would make selection ambiguous.
- `is_recording(owner)` means at least one recording exists for that owner.
- `_session_key(session)` locates the key for that specific session.
- `stop_recording(key)` and `send_current_part(key)` target one recording.

Every timer, call-ended event, shutdown task and `_stop_tasks` entry must target
one session/key. Never stop all of an owner's recordings when one call ends.
`_stop_tasks` is indexed by runtime `id(session)`, so an old upload/finalization
cannot block stopping a new recording that reuses the same `(owner, chat)`.
Remove these tasks only when the underlying finalization actually finishes;
cancelling a caller waiting on `asyncio.shield` must not start another finalizer.
A per-owner command/start lock serializes start requests; it does NOT limit the
number of already running recordings. Recording IDs come from SQLite history;
files use unique stems under `recordings/{owner}/`, and manifests stay per recording.

## Commands and selection

- `/login`, `/login_phone`, `/login_qr`, `/login_session`: primary account.
- `/addaccount <session> [label]`, `/accounts`, `/delaccount <pool ID>`: owned pool.
- `/join <username|invite> [--account ID]`: join with primary or selected owned pool
  account. This joins channel membership, not an additional voice call.
- `/record [chat reference] [--account ID]`: own account first, then free owned
  pool accounts. Private targets can be resolved through the account with access.
- `/status`: show all active recordings and their IDs.
- `/stop <chat ID>` or `/stop #<recording ID>`: stop exactly that recording.
- `/send <chat ID>` or `/send #<recording ID>`: deliver a preview, keep recording.
- `/stop all` / `/send all`: operate on this owner's recordings only.
- Bare `/stop` / `/send`: operate directly only when exactly one recording exists;
  show choices when there are several. Never silently select the first recording.
- `/stop waiting`: cancel the owner's waiting target without stopping recordings.
- `/logout`: require that owner's active recordings/start request to be stopped.

One pending waiting target per owner is supported. Other ACTIVE recordings can
continue alongside it. Waiting is persisted in `waiting_streams`, including an
optional selected pool account ID. Waiting does not reserve an account for hours;
capacity is checked atomically when the stream actually starts. The fallback
probe handles targets whose service messages the primary account cannot receive.
Scheduled call metadata also controls the probe: wake at schedule_date, then
probe every 5 seconds while the scheduled call is late. Service/raw schedule
changes wake the same wait via an Event; early live events still start immediately.
Restart restores the waiting target/account from DB and rediscovers the current
schedule from Telegram. A scheduled call is never treated as active based only
on wall-clock time. FloodWait pauses the probe for Telegram's required duration.

Account reservation transfer during failover is owned by StreamRecorder.
`recover_recording_account` returns a reserved replacement; it releases that
replacement if resolution fails or is cancelled. It must NOT free the previous
account before the recorder adopts the replacement. The recorder prepares the
new call client, rechecks capacity/finalizing state, swaps all session identity
fields together, then frees the previous account. Failure/cancellation before
adoption frees only the unused replacement. Cancellation of a status notification
after capture starts must not free the active recording's reservation.
`/stop all` re-reads active sessions after cancelling/awaiting pending starts and
waiting probes, including captures that committed just before cancellation.

## Media and async constraints

- Keep urllib; don't introduce requests/httpx into bot.py. Blocking Bot API,
  filesystem and encoding operations run through asyncio.to_thread or subprocesses.
- `/start` displays cached/stored login state without reconnecting to Telegram;
  account commands restore and validate clients when needed. Text sends use a
  10-second socket timeout and retry plain text only for Markdown parsing errors,
  avoiding duplicate sends on network timeouts. First polling timeout retries
  immediately; consecutive timeouts back off. Command age/duration logs omit arguments.
- `await call_client.calls` is correct: PyTgCalls exposes an async property.
- Capture frame dimensions from actual frame metadata; do not validate native
  frames using fixed VIDEO_WIDTH/HEIGHT. Capture uses native video parameters.
- AudioParameters.bitrate is PCM sample rate (48000), not AAC/Opus bitrate.
- FFmpeg file-processing commands use `-nostdin`; streaming encoders receive raw
  PCM/I420 via pipes. Preserve generation guards and bounded frame queues.
- Rolling parts near 1.97 GB upload while capture continues. `/send` is a preview;
  capacity chunks are retired once delivered and not duplicated in final output.
- Don't delete retained media/manifests after failed delivery or recovery.
- Bot API and MTProto media paths already handle video, audio and document fallback;
  do not restore obsolete instructions saying that only sendDocument is allowed.
- Main client registry is managed through `get_user_client`/activation helpers.
  Pool clients are managed by AccountManager. Don't disconnect a busy account.

## Diagnostics, configuration and security

- Errors/warnings go first to `LOG_CHANNEL_ID` (aliases LOGS_ID/LOG_EXCEPTION),
  then to OWNER_ID's private bot chat if channel/group delivery fails.
- Keep traceback, incident ID, user/command context, redaction, bounded queue,
  deduplication and recursive logging suppression. Never include command arguments
  containing passwords, phone codes, session strings or tokens in diagnostic context.
- `FORCE_SUB_ENABLED`, `SUBSCRIBE_CHANNEL`, `SUBSCRIBE_CHANNEL_URL` control optional
  membership requirements. Owner is exempt. Stop/status/logout/cancel/help remain
  accessible so users can stop recordings even if subscription checks fail.
- Credentials stay in `.env` and session storage, never documentation or commits.
- Tests must mock Telegram and isolate session/database paths. No real messages,
  channel joins or account logins as part of tests.

## Verification and change tracking

Read memory.md and BUGS.md before editing. Add newly discovered bugs with cause,
fix and regression coverage; record meaningful architecture decisions in memory.md.
Run `venv/bin/python -m unittest discover -s tests -t .` so tests/__init__.py installs
isolated config. FFmpeg-dependent tests may skip if binaries are unavailable.
Run `git diff --check`. Preserve pre-existing uncommitted changes and recordings.
Do not restart a live bot while recordings may be active just to validate code.
