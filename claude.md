# Context for Claude and other AI assistants

Read [AGENTS.md](AGENTS.md), [agents.md](agents.md), [memory.md](memory.md), and
[BUGS.md](BUGS.md) before changing this project. The shared architecture reference
is agents.md; this file intentionally points to it to avoid contradictory copies.

The main invariant is one Telegram account per simultaneous livestream, while one
bot user can have multiple accounts and record multiple different chats at once.
Active recording keys are `(owner_user_id, channel_id)`, not just a user ID.
