# PRD — #294 align /agent /mcp with CLI native config

**Issue**: #294 (epic, P1)
**Branch**: `refactor/294-cli-native-config`
**Status**: APPROVED

## Summary
Replace shadow config (data/agents.json + data/projects.json mcp_servers) with CLI-native storage (.claude/agents/*.md + .mcp.json).

## Subtask 1: /agent create → write .claude/agents/<name>.md
- Parse prompt + description into frontmatter + body format
- Write to `{project_path}/.claude/agents/<name>.md`
- Delete from data/agents.json if exists

## Subtask 2: /agent delete → remove .claude/agents/<name>.md
- Remove the .md file from project dir

## Subtask 3: /mcp add → write .mcp.json
- Read existing .mcp.json (or create new)
- Add server entry to mcpServers dict
- Write back atomically

## Subtask 4: /mcp remove → edit .mcp.json
- Remove server entry from mcpServers dict

## Subtask 5: Migration script
- On startup: migrate data/agents.json → .claude/agents/*.md
- On startup: migrate projects.json mcp_servers → .mcp.json
- After migration, stop writing to old locations

## AC
- AC1-AC8 per issue
