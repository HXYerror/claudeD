# claudeD command inventory (auto-generated)

## Slash command groups

| Group | Subcommand | Params | Description |
|---|---|---|---|
| /project | bind | path | Bind this channel to a local directory. |
| /project | info |  | Show this channel's current binding. |
| /project | unbind |  | Remove this channel's binding. |
| /project | system-prompt |  | Set system prompt for this project |
| /project | add-dir | path | Add extra directory access for Claude |
| /project | dirs |  | List extra directories |
| /project | remove-dir | path | Remove extra directory |
| /project | set-mode | mode | Set channel mode (thread or forum) |
| /project | set-mention-required | required | Toggle whether @ClaudeBot mention is required in this channel. |
| /project | set-root | path | Set per-guild projects root directory |
| /project | clear-root |  | Remove per-guild projects root override |
| /env | set | key, value | Set environment variable |
| /env | list |  | List environment variables |
| /env | remove | key | Remove environment variable |
| /session | stop |  | Stop the Claude session in this thread. |
| /session | clear |  | Drop context and start a fresh session in this thread (#163 sub-task 2). |
| /session | info |  | Show the current session's status. |
| /session | interrupt |  | Interrupt the current Claude operation in this thread |
| /session | resume |  | Resume the previous Claude session in this thread |
| /session | list |  | List all active Claude sessions |
| /session | compact |  | Compact the current session to save tokens |
| /session | fork |  | Fork the current session (new branch from same context) |
| /session | worktree | name | Create a git worktree for isolated work |
| /session | pin |  | Pin the last Claude reply |
| /session | name | name | Set session display name |
| /session | security-review |  | Run a security review on the current project |
| /session | settings | json_str | Apply custom settings JSON to session |
| /session | export |  | Export conversation history as markdown |
| /model | switch | name | Switch Claude model for this thread |
| /model | list |  | List available models + show current |
| /model | current |  | Show current Claude model for this thread |
| /mode | set | mode | Set Claude permission mode for this thread |
| /mode | cycle |  | Advance to the next permission mode in the fixed cycle order |
| /mode | current |  | Show the current permission mode + which tier it came from |
| /tools | allow | tools | Only allow specific tools |
| /tools | deny | tools | Deny specific tools |
| /tools | reset |  | Reset to default tools |
| /budget | set | amount | Set max budget per session (USD) |
| /budget | show |  | Show current budget setting |
| /budget | clear |  | Remove budget limit |
| /agent | create | name, prompt, description | Create a custom agent |
| /agent | list |  | List available agents |
| /agent | use | name | Use a custom agent in this thread |
| /agent | delete | name | Delete a custom agent |
| /mcp | add | name, command, args | Add a stdio MCP server |
| /mcp | add-url | name, url | Add an HTTP MCP server |
| /mcp | list |  | List configured MCP servers |
| /mcp | remove | name | Remove an MCP server |
| /skill | list |  | List skills available to the current channel. |
| /log | dump |  | Generate a diagnostic bundle and attach it to this thread. |
| /cost | show |  | Show cost for this channel |
| /cost | total |  | Show total cost across all channels |
| /cost | reset |  | Reset cost for this channel |
| /plugin | add | path | Add plugin directory and restart session |

## Top-level commands

| Name | Description |
|---|---|
| /effort | Set Claude's thinking effort level |
| /max-turns | Set maximum turns for Claude session |
| /fallback-model | Set fallback model for Claude session |
| /bare | Toggle bare/minimal Claude mode |
| /context | Visualize Claude context-window usage (current session or model baseline). |
| /diff | Show git diff (unstaged then staged) of the bound project. |
| /health | Show bot health and status |
| /review | Start a PR review session |
| /ratelimit | Show API usage stats |
| /debug | Toggle debug logging |
| /notify | Toggle pre-tool notifications on/off |
| /unbound-fallback | Toggle CLAUDED_ALLOW_UNBOUND_FALLBACK at runtime (admin; no restart needed). |
| /btw | Ask a quick side question without interrupting the main conversation. |
| /Send to Claude |  |
| /Pin Message |  |

**Total slash command surfaces: 69**

## on_message behaviors

- M1: mention-required + @bot in bound channel → new thread + session
- M2: mention-required=False + any msg → engage
- M3: unbound + @bot + fallback off → refuse hint once
- M4: unbound + @bot + fallback on → $HOME fallback
- M5: bot-created thread + plain msg → auto-resume session
- M6: 3rd-party thread + plain msg → silent unless @
- M7: testbot's own msgs → controlled by CLAUDED_TESTBOT_ID env
