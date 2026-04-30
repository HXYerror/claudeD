# claudeD — Discord-Claude Bridge

Expose [Claude Code](https://docs.anthropic.com/claude/docs/claude-code) through
a Discord bot. A Discord channel becomes a project (bound to a local directory),
and each thread inside it becomes its own Claude Code session — message Claude
in Discord, watch it stream code, tool calls, and answers back into the thread.

## How it works

```
+------------------+      +----------------+      +------------------+
| Discord channel  | ---> | claudeD bot    | ---> | Claude Code SDK  |
| (bound to dir)   |      | (this project) |      | (per-thread     )|
+------------------+      +----------------+      +------------------+
        |                       |                          |
        | new message creates   | persists channel→path    | streams assistant
        | a thread + session    | bindings to JSON         | messages + tool
        v                       v                          | results
+------------------+      +----------------+               v
| Discord thread   | <----+ DiscordRenderer + <----- AssistantMessage,
| (typewriter UX)  |      |                |          ToolUseBlock, etc.
+------------------+      +----------------+
```

Each Discord channel can be **bound** to a local project directory with
`/project bind`. Once bound, any top-level message in the channel opens a new
thread, and the thread becomes a Claude session whose `cwd` is the bound
directory. Subsequent messages in the thread continue that conversation. All
of Claude's text and tool activity is streamed back into the thread.

## Prerequisites

- **Python 3.13+**
- The **Claude Code CLI** installed and authenticated locally — the
  `claude-code-sdk` package shells out to it.
  See <https://docs.anthropic.com/claude/docs/claude-code> for install
  instructions, then run `claude` once to authenticate.
- A **Discord application + bot user** — create one at
  <https://discord.com/developers/applications> and copy the bot token.

## Installation

```bash
git clone <this-repo> discord-claude-bridge
cd discord-claude-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `clauded` console script.

## Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
# edit .env, set DISCORD_BOT_TOKEN
```

Available environment variables:

| Variable                  | Default              | Notes                                                        |
|---------------------------|----------------------|--------------------------------------------------------------|
| `DISCORD_BOT_TOKEN`       | (required)           | Bot token from the Discord developer portal.                 |
| `CLAUDE_MODEL`            | `sonnet`             | Default Claude model (`sonnet`, `opus`, `haiku`, or full id).|
| `CLAUDE_PERMISSION_MODE`  | `bypassPermissions`  | Claude Code permission mode (`default`, `acceptEdits`, `plan`, `bypassPermissions`). |

### Discord bot setup

In the Discord developer portal:

1. **Bot tab** — enable the **Message Content Intent** (privileged). Without
   it, the bot cannot read user messages and bridging will silently no-op.
2. **OAuth2 → URL Generator** — pick the `bot` and `applications.commands`
   scopes.
3. **Bot permissions** — at minimum:
   - View Channels
   - Send Messages
   - Send Messages in Threads
   - Create Public Threads
   - Read Message History
   - Embed Links
   - Use Application Commands
4. Open the generated URL and invite the bot to your server.

The bot itself requests these intents (see `bot.py`):

- `message_content` — to read message text
- `messages`        — to receive `MESSAGE_CREATE`
- `guilds`          — for slash command sync

## Running

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

State (channel→path bindings) is persisted to `./data/projects.json` in the
working directory the bot is launched from.

## Usage

1. **Bind a channel** to a local directory (one-time):

   ```
   /project bind path:/Users/me/code/my-project
   ```

   The path must already exist on the host running the bot.

2. **Start a Claude session** by sending a normal message in that channel.
   The bot opens a thread named after your message and connects a Claude
   session whose working directory is the bound path.

3. **Continue the conversation** by replying inside the thread. Each thread
   keeps its own independent Claude session.

4. **Inspect or stop** the session with `/session info` and `/session stop`
   inside the thread.

### Streaming UX

- Short replies (≲ 3 s) are sent as a single message.
- Longer replies are written into a "typewriter" message that's edited in
  place (~once per second) until it would exceed Discord's 2 000-character
  limit, at which point the current message is finalized and a new one is
  started. Code fences are auto-closed and reopened across splits.
- Tool calls render as `⚙️ Running: <name>…` status messages that update to
  `✅ <name>` or `❌ <name> failed` when the tool finishes.
- If Claude calls the **AskUserQuestion** tool, the bot renders the
  question(s) as Discord buttons (≤ 4 single-select options) or a select
  menu (multi-select / > 4 options) and waits up to 5 minutes for a click
  before timing out and denying the tool call.

## Commands reference

| Command            | Where     | What it does                                                  |
|--------------------|-----------|---------------------------------------------------------------|
| `/project bind`    | Channel   | Bind this channel to an absolute filesystem path. Validates that the directory exists. |
| `/project info`    | Channel   | Show the directory currently bound to this channel.           |
| `/project unbind`  | Channel   | Remove this channel's binding. Existing thread sessions stay running until stopped. |
| `/session info`    | Thread    | Show whether a Claude session is active in this thread, and its `cwd`. |
| `/session stop`    | Thread    | Disconnect the Claude session for this thread. The next message in the thread starts a fresh one. |

All command responses are ephemeral (only the invoker sees them).

## Architecture overview

Source layout (`src/clauded/`):

| Module                  | Responsibility                                                        |
|-------------------------|------------------------------------------------------------------------|
| `bot.py`                | Discord client; wires events and slash commands; owns the managers.    |
| `config.py`             | `.env` / environment loading into a frozen `Config` dataclass.         |
| `project_manager.py`    | Persisted channel-id → directory bindings (`data/projects.json`).      |
| `session_manager.py`    | In-memory map of thread-id → live `ClaudeBridge`.                      |
| `claude_bridge.py`      | Wraps a single `ClaudeSDKClient` connection (one per thread).          |
| `discord_renderer.py`   | Streams Claude's messages into Discord with smart-split + typewriter.  |
| `interaction_handler.py`| Renders `AskUserQuestion` tool calls as Discord buttons / selects.     |

Message flow on a top-level channel message:

1. `bot.on_message` checks the channel binding via `ProjectManager`.
2. A new thread is created with `message.create_thread(...)`.
3. `SessionManager.create_session(...)` constructs and starts a
   `ClaudeBridge` (one `ClaudeSDKClient`) for that thread, with an
   `InteractionHandler` wired in as the `on_ask_user` callback.
4. `DiscordRenderer.render_response(...)` consumes the SDK message stream
   and writes it to the thread.

Thread messages reuse the existing `ClaudeBridge` for that thread; if the
bridge has gone inactive (e.g. the SDK errored on a previous turn), a fresh
session is started transparently.

## Troubleshooting

**The bot ignores my messages in a channel.**
The channel isn't bound. Run `/project bind path:/abs/path` first. Bindings
are per-channel and survive bot restarts (`data/projects.json`).

**`/project bind` returns "Not a directory".**
The path must exist on the machine running the bot, not on your client.
Use an absolute path; `~` is expanded automatically.

**The bot replies but no message content appears.**
You probably haven't enabled the **Message Content Intent** in the Discord
developer portal. Enable it on the Bot tab and restart the bot.

**Claude session fails to start.**
Check the bot logs — usually means `claude` isn't on `PATH`, isn't
authenticated, or the bound directory isn't readable. Run `claude`
manually in that directory to verify. The thread will receive an
`❌ Failed to start Claude session: ...` message.

**Slash commands don't appear in Discord.**
Wait a minute — global command sync can take up to an hour the first time.
Re-invite the bot with the `applications.commands` scope if needed.

**Replies stop midway / "typewriter" message stops updating.**
Discord rate-limited us. The renderer backs off and retries automatically;
in the worst case a single edit is dropped but the stream continues. Check
the logs for `Discord edit rate-limited` warnings.

**Claude's process crashed mid-reply.**
The renderer surfaces `Error talking to Claude: …` in the thread, the
bridge is dropped, and the next message in the thread starts a fresh
session.

## Development

The full design lives in [`docs/prd/discord-claude-bridge.md`](docs/prd/discord-claude-bridge.md).

## License

MIT.
