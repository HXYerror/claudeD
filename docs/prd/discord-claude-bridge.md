# Discord-Claude Bridge (claudeD)

## Status
Done

## Overview

一个 Python 框架，将 Claude Code 的完整功能通过 Discord Bot 暴露给用户。用户在 Discord 中与 Claude Code 交互，体验等同甚至优于本地 CLI。

## Motivation

Claude Code CLI 功能强大但局限于终端。通过 Discord 桥接可以实现：
- 随时随地通过手机/桌面 Discord 使用 Claude Code
- 天然的会话管理（thread = session）
- 天然的项目隔离（channel = project）
- 丰富的 UI 组件（buttons、select menu）替代纯文本交互
- 会话历史自动持久化在 Discord 中

## Requirements

### R1 — 项目绑定
1. 一个 Discord channel 代表一个项目
2. 用 slash command `/project bind <path>` 将 channel 绑定到本地目录
3. 绑定信息持久化存储（重启不丢失）
4. 支持 `/project info` 查看当前绑定
5. 支持 `/project unbind` 解除绑定

### R2 — Session 管理
1. 用户在已绑定的 channel 中发送消息 → bot 自动创建 thread → 在 thread 中启动新 Claude session
2. thread 名称为消息内容的前 100 字符
3. 同一 thread 内的后续消息发送给同一 Claude session（多轮对话）
4. Claude session 的 `cwd` 设为 channel 绑定的项目目录
5. 使用 `claude-code-sdk` 的 `ClaudeSDKClient` 管理每个 session

### R3 — 消息桥接（Discord → Claude）
1. 用户在 thread 中发送文本消息 → 转发给对应的 Claude session
2. 用户上传的文件 → 下载到临时目录 → 作为上下文传递给 Claude
3. 支持在 Claude 处理中发送新消息（追加到 session）

### R4 — 消息桥接（Claude → Discord）
1. Claude 的文本回复 → 发送到对应 thread
2. 流式输出策略（混合模式）：
   - 回复在 3 秒内完成 → 等完成后一次性发送
   - 超过 3 秒 → 切换为打字机效果（每 1.2 秒编辑一次，末尾加 `▌` 光标）
   - 完成后去掉光标
3. 长消息分片：超过 1900 字符在合理断点（段落、代码块边界）拆分，发多条消息
4. 代码块保护：分片时不拆散未闭合的 ` ``` ` 块

### R5 — 工具调用展示
1. Claude 调用工具时，在 thread 中发送状态提示：`⚙️ Running: <tool_name>...`
2. 工具完成后更新状态：`✅ <tool_name> completed` 或 `❌ <tool_name> failed`
3. 状态消息简短，不展示完整输入输出（避免刷屏）

### R6 — AskUserQuestion 映射
1. Claude 使用 `AskUserQuestion` 工具时，拦截并桥接到 Discord
2. 单选（≤4 选项）→ Discord Buttons
3. 单选/多选（5-25 选项）→ Discord Select Menu
4. 用户在 Discord 选择后，将结果回传给 Claude session
5. 设置超时（5 分钟），超时自动回复超时错误

### R7 — 基础命令
1. `/project bind <path>` — 绑定 channel 到本地目录
2. `/project info` — 查看当前绑定信息
3. `/project unbind` — 解除绑定
4. `/session stop` — 停止当前 thread 的 Claude session
5. `/session info` — 查看当前 session 状态（cost、turns、model）

### R8 — 错误处理
1. Claude 进程崩溃 → 在 thread 中通知用户，提供重试按钮
2. 未绑定的 channel 发消息 → 提示用户先绑定
3. Discord rate limit → 自动退避重试
4. 网络断开 → 优雅降级，恢复后通知用户

### R9 — 配置
1. 通过 `.env` 文件配置：
   - `DISCORD_BOT_TOKEN` — Discord Bot Token
   - `CLAUDE_MODEL` — 默认模型（可选，默认 sonnet）
   - `CLAUDE_PERMISSION_MODE` — 权限模式（默认 bypassPermissions）
2. 项目绑定数据持久化到本地 JSON 文件

## Acceptance Criteria

- [ ] AC1: `/project bind ~/myproject` 后，在 channel 发消息能自动创建 thread 并得到 Claude 回复
- [ ] AC2: 同一 thread 中多轮对话保持上下文
- [ ] AC3: Claude 输出超过 2000 字符时正确分片
- [ ] AC4: Claude 执行 Bash/Edit 等工具时显示状态提示
- [ ] AC5: Claude 使用 AskUserQuestion 时，Discord 中出现 buttons/select menu，选择后 Claude 继续
- [ ] AC6: 短回复（<3s）一次性发送，长回复有打字机效果
- [ ] AC7: Bot 重启后绑定信息不丢失
- [ ] AC8: Claude 进程崩溃后用户收到通知

## Technical Approach

### 架构

```
Discord ←→ discord.py Bot
                ├── ProjectManager (channel ↔ 目录绑定)
                ├── SessionManager (thread ↔ ClaudeSDKClient 映射)
                │     ├── Session 1 (thread A → ClaudeSDKClient → ~/project1)
                │     ├── Session 2 (thread B → ClaudeSDKClient → ~/project1)
                │     └── Session 3 (thread C → ClaudeSDKClient → ~/project2)
                └── MessageBridge
                      ├── Discord → Claude (消息转发)
                      └── Claude → Discord (流式输出、分片、工具状态)
```

### 核心组件

1. **`bot.py`** — Discord Bot 入口，事件监听，slash command 注册
2. **`project_manager.py`** — Channel ↔ 目录绑定管理，JSON 持久化
3. **`session_manager.py`** — Thread ↔ ClaudeSDKClient 生命周期管理
4. **`claude_bridge.py`** — Claude SDK 封装，消息收发，工具拦截
5. **`discord_renderer.py`** — Claude 输出 → Discord 消息渲染（分片、流式、embed）
6. **`interaction_handler.py`** — AskUserQuestion ↔ Discord Buttons/Select Menu

### Claude SDK 使用方式

```python
from claude_code_sdk import ClaudeSDKClient, ClaudeCodeOptions

options = ClaudeCodeOptions(
    cwd="/path/to/project",
    permission_mode="bypassPermissions",
    model="sonnet",
)

async with ClaudeSDKClient(options) as client:
    await client.query("用户的消息")
    async for msg in client.receive_response():
        # 桥接到 Discord
        ...
```

### 流式输出策略

```python
async def stream_to_discord(response_iter, thread):
    buffer = ""
    first_token_time = None
    msg = None

    async for chunk in response_iter:
        buffer += chunk.text
        if first_token_time is None:
            first_token_time = time.time()

        elapsed = time.time() - first_token_time
        if elapsed < 3.0:
            continue  # 等一下，看能不能一次性发

        # 超过 3 秒，切换打字机模式
        if msg is None:
            msg = await thread.send(buffer[:1900] + "▌")
        elif time.time() - last_edit > 1.2:
            await msg.edit(content=buffer[:1900] + "▌")

    # 最终发送
    if msg:
        await msg.edit(content=buffer[:1900])  # 去掉光标
    else:
        await thread.send(buffer)  # 短回复，一次性发
```

### 工具拦截（AskUserQuestion）

通过 `can_use_tool` 回调拦截：

```python
async def handle_tool_permission(tool_name, tool_input, context):
    if tool_name == "AskUserQuestion":
        # 在 Discord 发 buttons/select menu
        # 等待用户选择
        # 返回 PermissionResultAllow(updated_input=用户选择)
        ...
    return PermissionResultAllow()  # 其他工具放行
```

### 数据持久化

```json
// data/projects.json
{
  "channel_id_1": {
    "path": "/Users/xuzhang/project1",
    "bound_at": "2026-04-30T17:00:00Z"
  }
}
```

Session 状态不持久化（重启后用户在 thread 里发新消息会创建新 session）。

## Testing Strategy

1. **单元测试**：消息分片逻辑、代码块检测、流式节流逻辑
2. **集成测试**：mock ClaudeSDKClient，验证消息桥接完整流程
3. **手动测试**：真实 Discord bot + Claude Code 端到端验证

## Out of Scope

- 多用户/权限管理（单用户场景）
- 沙箱隔离
- Web UI
- Claude 以外的 AI 模型支持
- 消息历史搜索
- 费用追踪/预算限制（v1 不做）
- Discord slash command 之外的 Claude slash command 映射（后续迭代）
