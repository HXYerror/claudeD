# claudeD — Discord-Claude Bridge

Expose [Claude Code](https://docs.anthropic.com/claude/docs/claude-code) through
a Discord bot. A Discord channel becomes a project (bound to a local directory),
and each thread becomes a Claude session.

## Requirements

- Python 3.13+
- A Discord bot token ([create one here](https://discord.com/developers/applications))
- The `claude` CLI installed and authenticated locally (used by `claude-code-sdk`)

## Install

```bash
git clone <this-repo>
cd discord-claude-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configure

Copy the example env file and fill in your values:

```bash
cp .env.example .env
# edit .env, set DISCORD_BOT_TOKEN
```

Available variables:

| Variable                  | Default              | Notes                                          |
|---------------------------|----------------------|------------------------------------------------|
| `DISCORD_BOT_TOKEN`       | (required)           | Bot token from the Discord developer portal.   |
| `CLAUDE_MODEL`            | `sonnet`             | Default Claude model.                          |
| `CLAUDE_PERMISSION_MODE`  | `bypassPermissions`  | Claude Code permission mode.                   |

## Run

```bash
clauded
# or, equivalently:
python -m clauded.bot
```

You should see something like:

```
INFO clauded.bot: Synced N application command(s)
INFO clauded.bot: Bot online as <name> (id=...)
```

## Slash commands

Currently registered (handlers are placeholders — wired in later subtasks):

- `/project bind <path>` — bind this channel to a local directory
- `/project info` — show the current binding
- `/project unbind` — remove the binding
- `/session stop` — stop the Claude session in this thread
- `/session info` — show the session's status

## Status

Skeleton only — issue #2. See `docs/prd/discord-claude-bridge.md` for the full
design.
