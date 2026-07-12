# PRD v2 — #292 Dynamic Workflow: /workflow commands + banner render

**Issue**: #292 (feat, P1)
**Branch**: `feat/292-workflow-v2`
**Status**: pending user approval

---

## 1. 目标

让用户在 Discord 里一眼看到 Dynamic Workflow 的完整生命周期（启动 banner → 进度 → 完成/失败），并能通过 `/workflow` 命令管理正在运行的 workflow。

---

## 2. Scope（Phase 1）

### 2.1 渲染升级

**启动 banner（TaskStartedMessage）**：紫色 embed + ⚡ 标识，视觉区分于普通工具

```
━━━━━━━━━━━━━━━━━━━━━━━
⚡ Dynamic Workflow Started
━━━━━━━━━━━━━━━━━━━━━━━
🔮 Task: <description[:60]>
🤖 Type: <task_type>
📋 ID: <task_id[:8]>
```

- 颜色: `COLOR_THINKING` (紫色 0x8B5CF6) — 区别于工具的黄色/蓝色
- 后续 progress 更新: embed 原地 edit，颜色变黄色 (running)
- 终态: 绿色 (completed) / 红色 (failed) / 灰色 (stopped/killed)

**进度更新（TaskProgressMessage）**：原地 edit 同一条 message

```
🔄 Dynamic Workflow Running
━━━━━━━━━━━━━━━━━━━━━━━
🔮 Task: <description[:60]>
💭 Last tool: <last_tool_name>
🪙 Tokens: <total_tokens> · 🔧 Tools: <tool_uses> · ⏱️ <duration>s
```

**终态（TaskNotification/TaskUpdated terminal）**：已有实现，保持

### 2.2 `_task_states` 提升

从 `render_response` 局部变量提升到 `DiscordRenderer` 实例属性 `self._task_states: dict[str, _TaskState]`。支持跨 turn 生存 + `/workflow` 命令查询。

### 2.3 `TaskUpdatedMessage` isinstance 化

SDK 0.2.110 已导出 `TaskUpdatedMessage`。移除 duck-type `_is_task_updated_message`，改用 `isinstance`。

### 2.4 AC6 drain

`ResultMessage` break 前加 `asyncio.wait_for` drain（5s timeout），继续收 Task* 消息直到超时或 `_task_states` 清空。

### 2.5 新 cog: `cogs/workflow.py`

| 命令 | 功能 |
|---|---|
| `/workflow list` | 列出 `_task_states` 中运行中的 task（embed 表格）|
| `/workflow kill <id>` | 调 `bridge.stop_task(task_id)` |
| `/workflow detail <id>` | 渲染 `_task_states[id]` 详情 embed |

### 2.6 `claude_bridge.py` 加 `stop_task` wrapper

```python
async def stop_task(self, task_id: str) -> None:
    await self._client.stop_task(task_id)
```

---

## 3. 文件变更

| 文件 | 变更 |
|---|---|
| `src/clauded/discord_renderer.py` | banner embed 样式 + `_task_states` 提升 + isinstance 化 + drain |
| `src/clauded/claude_bridge.py` | `stop_task()` wrapper |
| `src/clauded/cogs/workflow.py` | 新 cog: list/kill/detail |
| `src/clauded/bot.py` | setup_hook 注册 cog + 暴露 `_task_states` 访问 |
| `tests/test_workflow_render.py` | renderer Task* handler 测试 |
| `tests/test_cogs_workflow.py` | cog 命令测试 |

---

## 4. AC

- AC1: TaskStarted 渲染紫色 banner（⚡标识），一眼识别
- AC2: TaskProgress 原地 edit（节流 ≥ EDIT_INTERVAL_SECONDS）
- AC3: TaskNotification summary markdown 渲染
- AC4: TaskUpdated(killed) 收尾 embed（用 isinstance 不是 duck-type）
- AC5: 10+ 并发 task 不 crash（_safe_send/_safe_edit 兜底）
- AC6: ResultMessage 后 drain 5s 继续收 Task*
- AC7: 现有 subagent 渲染不回归
- AC8: `/workflow list` 显示运行中 task
- AC9: `/workflow kill <id>` 停止 workflow
- AC10: 启动 banner 视觉效果独特（紫色 + ⚡ + 分隔线）

---

## 5. Subtask 拆分（串行 dev sub-agent → 单 PR）

### Subtask 1: `_task_states` 提升 + isinstance 化 + banner render
- 提升到实例属性
- TaskUpdatedMessage isinstance
- 启动 banner 紫色 embed

### Subtask 2: AC6 drain + progress 样式
- ResultMessage break 前 drain
- progress embed 样式（黄色 running）

### Subtask 3: cog + bridge
- `claude_bridge.stop_task()`
- `cogs/workflow.py` 三命令
- bot.py 注册

### Subtask 4: 测试
- renderer handler tests (~15 cases)
- cog command tests (~5 cases)

---

## 6. Out of scope (Phase 2)

- `/workflow pause` / `resume` / `skip` / `retry`（SDK 无 API）
- workflow 历史查询（SDK 无 `list_sessions` API）
- workflow 脚本编辑/保存
