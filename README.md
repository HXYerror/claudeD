# claudeD — Discord-Claude Bridge

**Use Claude Code from anywhere — your phone, your tablet, your team's Discord server.**

claudeD bridges the full power of [Claude Code](https://docs.anthropic.com/en/docs/claude-code) to Discord. Each Discord channel maps to a project directory; each thread is an isolated Claude session. Mention the bot, get a thread, and interact with Claude exactly as you would from the CLI — streaming responses, tool execution, interactive prompts, and all. Full CLI parity.

---

## Features

### 🔗 Core — Message Bridging
- **Bidirectional message bridge** — Discord messages forwarded to Claude; Claude responses streamed back
- **Streaming output** — fast responses (<3s) sent in one shot; longer responses use a live typewriter effect with a `▌` cursor, edited every ~1.2s
- **Smart message splitting** — long responses split at paragraph/line/space boundaries, never mid–code-block
- **Code fence protection** — unclosed ` ``` ` blocks are closed-and-reopened across chunk boundaries
- **File attachments** — upload files in Discord and Claude can read them (images, code, docs)
- **Thread auto-creation** — mention the bot in a bound channel → thread created automatically, named after your message

### 📁 Project Management
- **`/project bind`** — bind a Discord channel to a local project directory
- **`/project system-prompt`** — set a persistent system prompt per project
- **`/project add-dir`** — grant Claude access to additional directories
- **`/mcp add` / `add-url`** — attach MCP (Model Context Protocol) servers (stdio or HTTP)
- **`/plugin add`** — load Claude Code plugins from a directory
- **Path security** — all paths validated against a configurable root; `..` traversal and symlink escapes rejected

### 🧵 Session Management
- **`/session resume`** — resume a previous session with full conversation context
- **`/session fork`** — branch a new session from the current conversation
- **`/session compact`** — compress context to save tokens (maps to Claude's `/compact`)
- **`/session worktree`** — create a git worktree for isolated work on a branch
- **`/session interrupt`** — interrupt Claude mid-operation
- **`/session stop`** — terminate the current session
- **`/session list`** — see all active sessions across the server
- **`/session info`** — view model, turns, and cost for the current session
- **`/session security-review`** — run Claude's built-in security review
- **`/session settings`** — apply custom settings JSON to the session
- **Auto-resume** — returning to a thread automatically resumes the previous session

### 🤖 Model Control
- **`/model`** — switch Claude model (sonnet, opus, haiku, or full model ID); starts a new session
- **`/effort`** — set thinking effort level (low / medium / high / xhigh / max)
- **`/fallback-model`** — set a fallback model for when the primary is overloaded
- **`/max-turns`** — cap the number of tool-use turns per response to prevent runaway loops

### 🔧 Tool Control
- **`/tools allow`** — whitelist specific tools (e.g. `Bash Edit Read`)
- **`/tools deny`** — blacklist specific tools (e.g. `WebSearch`)
- **`/tools reset`** — restore default tool permissions
- **`/budget set`** — set a per-session spending cap (USD)

### 🎨 Display & Rich UI
- **Colored embeds** — purple for Claude, yellow for running tools, green for success, red for errors, blue for info, gray for thinking
- **Diff preview** — `Edit` / `Write` tool calls show a formatted preview in Discord
- **File upload** — large code blocks (>3000 chars) automatically uploaded as file attachments
- **Thinking spoiler** — Claude's `ThinkingBlock` output rendered as a spoiler-tagged embed
- **Plan mode** — `EnterPlanMode` / `ExitPlanMode` shown as status embeds
- **Subtask display** — `Task` tool calls rendered with description
- **Todo list** — `TodoWrite` renders a checkbox-style todo list embed
- **PreToolUse hooks** — "🔮 Preparing: ToolName…" notification appears *before* tool execution
- **Interactive prompts** — `AskUserQuestion` → Discord buttons (≤4 options) or select menus (5–25 options) with 5-minute timeout
- **Crash recovery** — error embed with a 🔄 Retry button to restart the session
- **Cost footer** — every response shows cost, tokens, turns, model, and duration

### 🤖 Custom Agents
- **`/agent create`** — define a named agent with a custom system prompt and description
- **`/agent use`** — activate an agent in the current thread
- **`/agent list`** — list all defined agents
- **`/agent delete`** — remove an agent definition
- Agents persist across bot restarts (JSON storage)

### 💰 Cost Tracking
- **`/cost show`** — per-channel cumulative cost and call count
- **`/cost total`** — global cost across all channels
- **`/cost reset`** — reset a channel's cost counter
- Per-response cost shown in the response footer
- Costs persisted to disk (survives restarts)

### 🏥 Health & Operations
- **`/health`** — uptime, active sessions, bound projects, Claude CLI version, Python version
- **`/review`** — start a PR review session (creates a thread with `--from-pr`)
- Graceful fallback if Message Content Intent is not enabled (slash commands still work)

---

## Prerequisites

- **Python 3.13+**
- **Claude Code CLI** installed and authenticated — verify with `claude --version`
- **Discord bot token** from the [Discord Developer Portal](https://discord.com/developers/applications)
- **Discord bot permissions:**
  - Send Messages
  - Create Public Threads
  - Manage Channels
  - Read Message History
  - Embed Links
  - Attach Files
  - Use Slash Commands
- **Message Content Intent** enabled in the bot's Developer Portal settings (Bot → Privileged Gateway Intents → Message Content Intent)

---

## Installation

```bash
git clone https://github.com/HXYerror/claudeD.git
cd claudeD
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .
```

---

## Configuration

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | **Yes** | — | Your Discord bot token |
| `CLAUDE_MODEL` | No | `sonnet` | Default Claude model (`sonnet`, `opus`, `haiku`, or full model ID) |
| `CLAUDE_PERMISSION_MODE` | No | `default` | Permission mode: `default`, `acceptEdits`, `plan`, `bypassPermissions` |
| `CLAUDED_PROJECTS_ROOT` | No | `~` (home dir) | Root directory for project bindings — paths outside this are rejected |
| `CLAUDED_ALLOW_UNBOUND_FALLBACK` | No | `false` | When `1`/`true`, `@bot` in an unbound channel falls back to `$HOME` as `cwd`. When unset/`false` (default), the message is silently ignored — run `/project bind <path>` first. ⚠️ See security warning below |

> ⚠️ **Security warning:** Setting `CLAUDE_PERMISSION_MODE=bypassPermissions` disables all tool-permission prompts. Claude will execute shell commands and file edits without confirmation. Only use this if every Discord user with channel access is trusted with shell access on the host machine.

> ⚠️ **Security warning:** Setting `CLAUDED_ALLOW_UNBOUND_FALLBACK=1` lets *any* user with channel-write permission run Claude with `cwd=$HOME` — including reading `~/.ssh`, `~/.aws`, etc. Unlike `/project bind`, the fallback path is *not* validated against `CLAUDED_PROJECTS_ROOT` and *does not* require Discord administrator. Only enable if every potential message author is trusted with read access to the operator's home directory. The default (`false`) is the v1.0 behavior: unbound channels ignore `@bot`.

---

## Quick Start

1. **Start the bot:**
   ```bash
   clauded
   ```

2. **Invite the bot** to your Discord server using the OAuth2 URL from the Developer Portal (with the permissions listed above).

3. **Bind a channel** to a project directory:
   ```
   /project bind /path/to/your/project
   ```

4. **Mention the bot** with your request:
   ```
   @ClaudeBot refactor the auth module to use JWT tokens
   ```

5. The bot creates a thread, starts a Claude session, and streams the response. Continue the conversation in the thread — context is preserved across messages.

---

## Commands Reference

### Project Management

| Command | Description | Example |
|---|---|---|
| `/project bind <path>` | Bind this channel to a local directory | `/project bind /home/user/myapp` |
| `/project info` | Show current binding, system prompt, and extra dirs | `/project info` |
| `/project unbind` | Remove this channel's project binding | `/project unbind` |
| `/project system-prompt <text>` | Set a system prompt (use `clear` to remove) | `/project system-prompt You are a Go expert` |
| `/project add-dir <path>` | Add extra directory access for Claude | `/project add-dir /home/user/shared-libs` |
| `/project dirs` | List all extra directories | `/project dirs` |
| `/project remove-dir <path>` | Remove an extra directory | `/project remove-dir /home/user/shared-libs` |

### Session Management

| Command | Description | Example |
|---|---|---|
| `/session info` | Show model, turns, cost for current session | `/session info` |
| `/session stop` | Stop the Claude session in this thread | `/session stop` |
| `/session interrupt` | Interrupt Claude mid-operation | `/session interrupt` |
| `/session resume` | Resume a previous session with full context | `/session resume` |
| `/session fork` | Fork a new session from current conversation | `/session fork` |
| `/session compact` | Compress context to save tokens | `/session compact` |
| `/session worktree <name>` | Create a git worktree for isolated work | `/session worktree feature/auth` |
| `/session list` | List all active sessions | `/session list` |
| `/session security-review` | Run Claude's built-in security review | `/session security-review` |
| `/session settings <json>` | Apply custom settings JSON | `/session settings {"key": "value"}` |

### Model & Effort

| Command | Description | Example |
|---|---|---|
| `/model <name>` | Switch Claude model (restarts session) | `/model opus` |
| `/effort <level>` | Set thinking effort: low, medium, high, xhigh, max | `/effort max` |
| `/max-turns <number>` | Cap tool-use turns per response | `/max-turns 10` |
| `/fallback-model <model>` | Set fallback model for overload | `/fallback-model haiku` |

### Tool Control

| Command | Description | Example |
|---|---|---|
| `/tools allow <tools>` | Only allow listed tools (space-separated) | `/tools allow Bash Edit Read` |
| `/tools deny <tools>` | Deny listed tools (space-separated) | `/tools deny WebSearch WebFetch` |
| `/tools reset` | Reset to default tool permissions | `/tools reset` |

### Budget

| Command | Description | Example |
|---|---|---|
| `/budget set <amount>` | Set max session budget in USD | `/budget set 5.00` |
| `/budget show` | Show current budget setting | `/budget show` |
| `/budget clear` | Remove budget limit | `/budget clear` |

### Cost Tracking

| Command | Description | Example |
|---|---|---|
| `/cost show` | Show cost for this channel | `/cost show` |
| `/cost total` | Show total cost across all channels | `/cost total` |
| `/cost reset` | Reset this channel's cost counter | `/cost reset` |

### Custom Agents

| Command | Description | Example |
|---|---|---|
| `/agent create <name> <prompt> [desc]` | Create a custom agent | `/agent create reviewer "Review code for bugs" "Code reviewer agent"` |
| `/agent list` | List all defined agents | `/agent list` |
| `/agent use <name>` | Activate an agent in this thread | `/agent use reviewer` |
| `/agent delete <name>` | Delete an agent definition | `/agent delete reviewer` |

### MCP Servers

| Command | Description | Example |
|---|---|---|
| `/mcp add <name> <command> [args]` | Add a stdio MCP server | `/mcp add myserver npx my-mcp-server` |
| `/mcp add-url <name> <url>` | Add an HTTP MCP server | `/mcp add-url remote https://mcp.example.com` |
| `/mcp list` | List configured MCP servers | `/mcp list` |
| `/mcp remove <name>` | Remove an MCP server | `/mcp remove myserver` |

### Plugins

| Command | Description | Example |
|---|---|---|
| `/plugin add <path>` | Add a plugin directory | `/plugin add /home/user/my-plugin` |

### Other

| Command | Description | Example |
|---|---|---|
| `/health` | Show bot health, uptime, and versions | `/health` |
| `/review <pr>` | Start a PR review session in a new thread | `/review 42` or `/review https://github.com/org/repo/pull/42` |

---

## `/schedule` — Timer system

Create timers that fire at a scheduled time and either inject a user message into an existing thread's session, or spawn a new thread + fresh claude session to run an independent task. Schedules persist across bot restarts.

### Subcommands

| Command | Purpose |
|---|---|
| `/schedule message <text>` | Inject reminder; claude parses → calls `schedule_message` MCP tool → schedule persisted |
| `/schedule new_task <text>` | Spawn a new thread + session at fire time; claude calls `schedule_new_task` |
| `/schedule list [scope]` | List schedules (scope = `thread` / `channel` / `all`) — directly reads store, no claude turn |
| `/schedule delete <id>` | Delete by 16-char hex id or 8-char prefix (creator or admin) |
| `/schedule toggle <id> <enabled>` | Enable / disable a schedule (creator or admin) |

### Triggers

Two forms (claude converts your natural language into one of these and passes via the MCP tool):
- **One-shot**: `iso: 2026-05-20T09:00:00+08:00`
- **Recurring cron**: `cron: 0 9 * * *` (5-field; interpreted in the channel's tz, default `Asia/Shanghai`)

### `max_lifetime` (claude-only)

When creating from natural language ("提醒我一个月"), claude can set `max_lifetime` as a duration string (`30d`, `7d`, `24h`, max **365d**). Counts from first fire. Only valid with `recurring=true`. Not exposed in the slash UI — set via prompt only.

### Caps

- **per-user active**: 20 schedules
- **global active**: 100 schedules
- **claude min interval (cron)**: 5 minutes
- **global in-flight fires**: 10 concurrent

### Permissions

| Action | Allowed |
|---|---|
| Slash create (`message` / `new_task`) | Anyone in bound channel |
| Slash list | Anyone |
| Slash delete / toggle | Creator + admin |
| Tool create | Claude (auto with `created_by="claude"`) |
| Tool delete / toggle | Claude can only touch claude-created |

### Storage

`data/schedules.json` — atomic write (mirrors `data/sessions.json`). Corrupt JSON falls back to an empty store with a `WARNING` log.

### Behavior

- **Fire visibility**: When a schedule fires, the target thread receives a `-# ⏰ Scheduled fire: <name>` prefix line followed by the injected text shown as a Discord block quote (`> ...`) so users see exactly what was sent into the conversation.
- **`new_task` fires**: parent channel gets a `📌 Scheduled-task thread created` embed announcing the new thread.
- **Missed fires across restart**: ≤5 minutes late → fire on next tick; >5 minutes late → mark `missed`, log WARNING, roll `next_fire_at` forward, do NOT fire.
- **Terminal errors** (`NotFound` / `Forbidden`): auto-disable + log.
- **Transient errors**: 1s / 4s / 16s backoff, 3 attempts, then disable.
- **`max_lifetime` expiry**: auto-disable + post an `⏰ Schedule expired` embed to the schedule's original created channel.

Spec / PRD: `docs/prd/v1.18-scheduler.md`. Issue: #241.

---

## Architecture

```
Discord ←→ ClaudedBot (discord.py)
              │
              ├── ProjectManager      channel ↔ directory binding, system prompts,
              │                       extra dirs, MCP servers, budgets (JSON persistence)
              │
              ├── SessionManager      thread ↔ ClaudeBridge lifecycle, per-thread locks,
              │                       session persistence & resume
              │
              ├── ClaudeBridge        SDK wrapper: ClaudeSDKClient connection, message
              │                       streaming, tool permission callbacks, interrupt
              │
              ├── DiscordRenderer     streaming output: typewriter mode, smart splitting,
              │                       code fence protection, tool embeds, diff previews,
              │                       file uploads, thinking spoilers, plan/task/todo
              │
              ├── InteractionHandler  AskUserQuestion → Discord buttons / select menus
              │                       with timeout and multi-select support
              │
              ├── CostTracker        per-channel and global cost tracking (JSON persistence)
              │
              ├── SessionStore       session ID persistence for resume across restarts
              │
              └── AgentManager       custom agent CRUD with JSON persistence
```

### Data Flow

```
User @mentions bot in #my-project channel
  → Bot creates thread "refactor the auth module..."
    → SessionManager creates ClaudeBridge(cwd=/path/to/project)
      → ClaudeSDKClient connects to Claude Code CLI
        → User messages streamed to Claude
        → Claude responses streamed back through DiscordRenderer
          → Text → typewriter messages
          → ToolUse → colored status embeds
          → AskUserQuestion → InteractionHandler → buttons/menus
          → ResultMessage → cost footer
```

---

## Color Scheme

| Color | Hex | Usage |
|---|---|---|
| 🟣 Purple | `#7C3AED` | Claude's text replies |
| 🟡 Yellow | `#F59E0B` | Tool currently executing |
| 🟢 Green | `#10B981` | Tool completed successfully |
| 🔴 Red | `#EF4444` | Tool failed / error |
| 🔵 Blue | `#3B82F6` | Info messages, commands, plan mode |
| ⚪ Gray | `#6B7280` | Thinking blocks (spoiler-tagged) |

---

## Troubleshooting

| Problem | Solution |
|---|---|
| **Bot doesn't respond to messages** | Enable **Message Content Intent** in Discord Developer Portal → Bot → Privileged Gateway Intents |
| **"DISCORD_BOT_TOKEN is not set"** | Copy `.env.example` to `.env` and add your bot token |
| **"claude not found" on session start** | Install Claude Code CLI: `npm install -g @anthropic-ai/claude-code` and verify with `claude --version` |
| **Bot can't create threads** | Ensure the bot has **Create Public Threads** and **Send Messages** permissions in the channel |
| **"Path is outside the allowed projects root"** | The path must be under `CLAUDED_PROJECTS_ROOT` (defaults to `~`). Adjust the env var or use an allowed path |
| **Rate limit errors / edits failing** | This is normal under heavy load — the renderer auto-backs-off. Discord limits message edits to ~5/s |
| **Session crashes with retry button** | Click 🔄 Retry — a fresh session is created automatically. Check logs for the root cause |
| **Slash commands not appearing** | Commands sync on startup. Wait ~1 minute, or restart the bot. Guild-level sync can take up to an hour |
| **Bot starts but slash commands fail** | Make sure the bot was invited with the **Use Slash Commands** (applications.commands) scope |
| **Permission mode warnings** | If using `bypassPermissions`, ensure only trusted users can post in bound channels |

---

## Run as macOS service

claudeD ships with a user-level LaunchAgent so the bot starts at login, auto-restarts on crash, rotates its log file, and surfaces a desktop notification if it crash-loops. macOS only — Linux/Windows users should run `clauded` under their own service manager (systemd, NSSM, etc.).

### Prerequisites

- `.env` exists at the repo root with `DISCORD_BOT_TOKEN` set
- `.venv` is built and `pip install -e .` has been run so `.venv/bin/clauded` exists and is executable

### Install

```bash
./scripts/install-launchagent.sh
```

The script renders the plist templates with absolute paths for your `$HOME` and repo, drops them under `~/Library/LaunchAgents/`, and bootstraps both agents (`com.hxy.clauded` and `com.hxy.clauded.healthcheck`) into your launchd user domain. The bot should be online in Discord within 30 seconds.

### Status & logs

```bash
# Is launchd running it?
launchctl print gui/$(id -u)/com.hxy.clauded

# Live app log (rotates at 10 MB × 7 backups)
tail -f ~/Library/Logs/clauded/clauded.log

# launchd stdout/stderr (boot diagnostics)
tail -f ~/Library/Logs/clauded/out.log ~/Library/Logs/clauded/err.log

# Crash-loop alerts (≥3 restarts in 5 min)
tail -f ~/Library/Logs/clauded/alerts.log
```

### Uninstall

```bash
./scripts/uninstall-launchagent.sh
```

Removes the plists and unloads both agents. Logs under `~/Library/Logs/clauded/` are preserved for audit; delete that directory manually if you want them gone.

### Notes

- macOS only. The plist templates use `launchctl bootstrap` (modern, macOS 10.10+).
- Re-run `./scripts/install-launchagent.sh` after upgrading Python or recreating `.venv` to refresh the plist's absolute path.
- The bot writes a heartbeat to `~/Library/Caches/clauded/heartbeat` every 30 s; the helper agent polls every 5 min and `launchctl kickstart`s the main agent if the heartbeat is stale (>120 s) — covering silent Discord-gateway hangs that `KeepAlive` alone won't catch.

---

## Development

Install with development dependencies:

```bash
pip install -e ".[dev]"
```

Run the test suite:

```bash
python -m pytest tests/ -v
```

The project includes **138 tests** covering:
- Smart message splitting and code fence protection
- Diff display rendering
- Project manager bindings and path security
- Session manager lifecycle and locking
- Session store persistence
- Cost tracker arithmetic and persistence
- Agent manager CRUD
- Claude bridge configuration and callbacks
- SDK hooks and partial message streaming
- Channel markers detection
- Bot mention stripping
- Image attachment handling
- Startup smoke tests and config loading

---

## License

[MIT](LICENSE)
