# PRD — #245 raw reply/send/reaction silent drops

**Issue**: #245 (bug, P1)  
**Branch**: `fix/245-safe-send`
**Status**: APPROVED

## Problem
7 raw Discord API calls in bot.py (`message.reply`, `channel.send`, `message.add_reaction`) silently fail on transient network blips (ConnectionResetError, SSL reset). Existing safe wrappers (`safe_send_message`, `safe_add_reaction` in `_http_retry.py`) handle retries + logging but aren't used at these 7 sites.

## Fix
Replace each raw call with the safe wrapper. The wrappers already exist — this is a wiring fix.

## Sites (from issue audit)

1. `bot.py:~614` — `message.reply(UNBOUND_REFUSE_MESSAGE)` → `safe_send_message(message.channel, content=UNBOUND_REFUSE_MESSAGE, reference=message)`
2. `bot.py:~657` — `channel.send("❌ I don't have permission...")` → `safe_send_message(channel, content="❌ ...")`
3. `bot.py:~706` — `channel.send("❌ Failed to create a thread...")` → `safe_send_message(channel, content="❌ ...")`
4. `bot.py:~790` — `message.add_reaction("⏳")` → `safe_add_reaction(message, "⏳")`
5. `bot.py:~904` — `message.reply(UNBOUND_REFUSE_MESSAGE)` → same as #1
6. `bot.py:~1000` — `message.add_reaction("⏳")` → `safe_add_reaction(message, "⏳")`
7. Any other raw `channel.send(embed=embed)` in fire callbacks that aren't already safe

## Tests
- Mock `message.reply` to raise `aiohttp.ClientConnectorError` → assert no exception escapes, log WARNING emitted
- Verify safe wrappers are used (grep-based assertion: no raw `message.reply` / `channel.send` / `add_reaction` outside of `_http_retry.py` and `discord_renderer.py`)

## AC
- AC1: ConnectionResetError on any of the 7 sites → no unhandled exception, bot continues
- AC2: grep audit: 0 raw calls remaining in bot.py (outside renderer which has its own retry)
