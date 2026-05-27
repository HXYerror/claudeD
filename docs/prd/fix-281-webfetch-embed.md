# PRD — #281 WebFetch/WebSearch completion embed preserves URL/query

**Issue**: #281 (bug, P2)
**Branch**: `fix/281-webfetch-embed`
**Status**: APPROVED

## Problem
WebFetch/WebSearch completion embed replaces the launching embed (which had URL/query) with a title-only embed, losing the URL/query info.

## Fix
When updating the tool embed on completion, preserve the original description (URL or query).

## Files
- `src/clauded/discord_renderer.py` — find the ToolResultBlock handling for WebFetch/WebSearch completion embeds, keep description

## AC
- AC1: WebFetch completion shows URL
- AC2: WebSearch completion shows query
