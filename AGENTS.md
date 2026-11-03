# Telegram livestream recorder — instructions for AI agents

Read [agents.md](agents.md) before changing code. It is the current architecture
reference for every AI assistant, including Codex, Claude, and other tools.
Read [memory.md](memory.md) and [BUGS.md](BUGS.md) for decisions and known bugs.

Critical policy (2026-11-03): **one Telegram account = one active livestream;
one bot user may own multiple accounts and record multiple livestreams at once.**
Never reinstate a one-recording-per-user limit. Runtime recording keys are
`(owner_user_id, channel_id)`. Timers, stop, events, failover and uploads must
operate on the selected recording. Authenticate ownership before account or
recording selection. Keep existing active recordings running when starting,
stopping, or delivering another recording.

Validation: `venv/bin/python -m unittest discover -s tests -t .` (isolated config;
no real Telegram messages). Do not restart a live recorder just to run tests.
