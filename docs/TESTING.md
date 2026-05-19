# claudeD 测试体系总览

本文档总结了 claudeD 项目所有测试类型 — 测什么、怎么测、跑在哪、能覆盖什么、不能覆盖什么。

最后更新：2026-05-19（PR #258 + #259 之后，e2e #251 epic 闭合）。

---

## 总览表

| 层 | 类型 | 数量 | 跑在哪 | 时长 | 真 Discord? | 真 Claude SDK? |
|---|---|---:|---|---|:---:|:---:|
| 1 | **Unit tests** | 1071 + 17 xfail | pytest (CI) | ~2 min | ❌ | ❌ |
| 2 | **Mock e2e harness** | 118 cases | pytest 或 `run_e2e.py` | ~38 s | ❌ | 部分 |
| 3 | **Real Discord e2e** | 9 cases (7 PASS) | `run_real_e2e.py` 手动 | ~3 min | ✅ | ✅ |
| 4 | **诊断 / probe 脚本** | 多个 ad-hoc | 手动 | 秒级 | 视情况 | ✅ |

总测试规模：**~1200 cases** 全自动 + **9 真实 Discord** 端到端。

---

## 1. Unit Tests — `tests/test_*.py`

### 测什么

**模块级行为契约**：每个 module 内函数 / 方法的输入输出、边界、异常路径。**所有 cog 的回调逻辑**也覆盖（通过构造 mock interaction 调用 callback）。

### 怎么测

- pytest + `pytest-asyncio`
- 大量 `MagicMock` / `AsyncMock` 替换 Discord 客户端 / Claude SDK
- 用 `inspect.getsource()` 做 source-grep 锁住代码 invariant（防回归 — 例如 `#226 R1`: 强制 `matched = True` 一定要在所有分支中出现）
- conftest.py 共享 fixtures (`FakeBridge` / `FakeTarget` / `_zero_context_settle_delay` autouse)

### 跑

```bash
PATH=/opt/homebrew/bin:$PATH PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

CI 期望 **1071 passed, 17 xfailed** (xfail = e2e suite 的 known-bug reproducer)。

### 覆盖

| 类别 | 文件 | 测试数 |
|---|---|---:|
| 渲染 (typewriter / table / footer / tool result) | `test_renderer_*`, `test_tool_result_shorttier`, `test_subtask_complete_render`, ... | ~200 |
| 会话管理 / 持久化 | `test_session_*`, `test_project_manager` | ~120 |
| Cog 命令 (mock Interaction) | `test_*_cmd`, `test_permission_mode`, `test_model_default`, ... | ~280 |
| Image / 字体 / 表格 | `test_image_preprocess`, `test_image_attach`, `test_cjk_font_tofu`, `test_table_*` | ~90 |
| 诊断基础 | `test_log_dump_bundle`, `test_stream_logger`, `test_logger_instrumentation` | ~80 |
| 错误 / 重试 / 恢复 | `test_renderer_retries`, `test_cascade_resilience`, `test_retry_resume` | ~50 |
| 工具子集 (binding id, smart split, 等等) | `test_binding_id_resolution`, `test_smart_split`, ... | ~250 |

### 不能覆盖

- Discord 网关 / gateway dispatch
- 真实 Discord HTTP 限速 / 5xx
- Claude SDK 子进程 (CLI binary) 实际行为
- `app_commands.Choice` 服务端 enforcement（mock 用真 `Choice` 对象绕过）
- 真实 image attachment 经 Discord HTTP 上传

### 文件位置

```
tests/
├── conftest.py                     # 共享 fixtures
├── test_e2e_suite.py               # 将 Mock e2e harness 适配到 pytest
└── test_*.py                       # 73 个单元测试文件
```

---

## 2. Mock E2E Harness — `scripts/e2e/run_e2e.py`

### 测什么

**Cog 回调 + 后端组件全链**：从 cog callback 入口（用 Mock Interaction 驱动）到 ProjectManager / SessionStore / CostTracker / AgentManager 真实读写 `/tmp/e2e_data/*.json`，再到 bot 回复内容（捕获 `interaction.response.send_message` / `followup.send` 调用参数 + embed 字段）。

### 怎么测（Approach D）

1. **Bot 构造**: `ClaudedBot.__new__(ClaudedBot)` 绕过 `__init__` (Discord 客户端 setup) — 关键点是用 **真 ClaudedBot subclass**，不是 MagicMock，这样 cog 里的 `isinstance(bot, ClaudedBot)` 通过。
2. **Mock Interaction**: 自建 `discord.Interaction` 替身 — `response.send_message` / `followup.send` 用 AsyncMock 捕获参数到列表。Critical: `response.is_done = lambda: False` 强制走主分支（不是 MagicMock truthy）。
3. **数据隔离**: 真实 ProjectManager / CostTracker / SessionStore / AgentManager 实例，data_dir 指向 `/tmp/e2e_data/`，每个 case fresh dir。
4. **严格断言**: 每个 case 都验证**具体输出字符串 / 状态变化 / 磁盘 JSON 内容**，不只是"reply 非空"。

### 跑

```bash
# 独立运行（详细输出 + 报告）
PYTHONPATH=src python scripts/e2e/run_e2e.py --phase happy

# 作为 pytest 一部分跑（CI 友好）
PYTHONPATH=src pytest tests/test_e2e_suite.py -v
```

输出：`data/e2e-reports/YYYY-MM-DD_HHMMSS.md` markdown 报告。

### 当前结果

**118 cases**：
- **101 PASS** (88%)
- **17 XFAIL / FAIL** — 全部是 open bug 反向 reproducer
- **0 ERROR**
- 运行 ~38 秒

### 覆盖矩阵

| Cog | 命令数 | 测试 case 数 | edge case 类型 |
|---|---:|---:|---|
| /project | 11 | 17 | DM, unbound, relative-path, nonexistent, traversal, thread→parent(#197), set-mode invalid/valid, set-root invalid/valid, dirs empty/after-add, set-mention-required true/false |
| /env | 3 | 7 | set+list round-trip, list unbound, set unbound, remove unknown/happy |
| /session | 14 | 17 | 每个子命令 no-session 路径 + worktree/name/settings/security-review thread enforcement |
| /agent | 4 | 7 | create+list, list empty, delete happy/unknown, use unknown/happy, create unbound, **create duplicate (#254)**, **create empty-name (#255)** |
| /mcp | 4 | 7 | add stdio happy, add-url happy, list empty/unbound, remove unknown, add unbound, **add empty-name (#255)** |
| /model | 3 | 6 | switch/list/current happy/unknown, **list freshness (#247)** |
| /mode | 3 | 3 | set/cycle/current no-session |
| /tools | 3 | 3 | allow happy, deny/reset not-thread |
| /budget | 3 | 4 | set/show/clear, show with-value, **show/clear unbound (#257)** |
| /cost | 3 | 5 | show/total/reset, show with planted cost, **record zero gate (#248)**, **record race (#252)** |
| /ops | 8 | 11 | health, ratelimit, debug toggle-twice, notify, unbound-fallback, btw, review |
| /context | 1 | 1 | no-session 显示 fresh baseline |
| /diff | 1 | 1 | unbound refuse |
| /log dump | 1 | 4 | happy 真打包 + zip 结构验证, bundle-fail, concurrent ×5, DM |
| /skill | 1 | 2 | list signature + no-bridge |
| /effort, /max-turns, /fallback-model, /bare | 4 | 4 | 边界值 (negative / zero / huge) + happy |
| **DM 边缘** | — | 3 | /session list, /health, /log dump in DM |
| **并发 / fault injection** | — | 8 | bind race, agent×20 concurrent, log×5 concurrent, cost record×30 (#252), session resume after restart, corrupt-JSON file recovery, save/load roundtrip |

### 关键约定

- **每个 case 完全隔离**：fresh bot + fresh `/tmp/e2e_data/`，无 cross-test pollution。
- **每个 PASS 都是严格断言**：要么验证字符串 / regex，要么验证状态对象变化，要么验证磁盘 JSON 写入。"reply 非空"不算 PASS。
- **XFAIL 即 bug**：所有 17 个 FAIL 都有对应 open issue（#247 #248 #252 #254 #255 #257），fix 后 case 自动 PASS（pytest 会 warn "unexpected pass"）。

### 不能覆盖

- Discord 网关 dispatch (mock Interaction 不走 gateway)
- 真实 Claude CLI 子进程（除非 case 显式调用 SDK；目前 `/context`、`/skill list`、`/review` 因为代码路径使然，确实跑了真 CLI — 这是少数）
- discord.py interaction lifecycle race conditions
- 真 attachment 上传 (Discord HTTP)
- 真 thread 创建 / archive

---

## 3. Real Discord E2E — `scripts/e2e/run_real_e2e.py`

### 测什么

**用真账号 (TesterBot) 在真 Discord guild + 真 channel 里发消息和附件，看真生产 bot (pid alive 在 launchd 跑的那个) 的真实响应**。

### 怎么测（Approach E）

1. **Setup**:
   - 读 `.testbot.env.txt` 拿 TesterBot token
   - 验证 `#testbot` channel 绑定（自动加临时 binding 若没有；测完恢复）
   - 若加了 binding，`launchctl kickstart -k gui/501/com.hxy.clauded` 让 bot 重载 `projects.json`
2. **驱动**: testbot client 连 Discord，在 `#testbot` 发消息 / 附件
3. **观察**: 轮询 `channel.history()` 看 bot 的真回复 (channel 里 + bot 创建的 thread 里)
4. **验证**: 内容 substring 匹配、attachment 存在、thread 创建、bot 日志文件 grep（验证生产 bot 真跑了某段代码）
5. **Teardown**: 恢复 `projects.json`、archive 测试 thread

### 跑

```bash
# 前置条件：
# - bot 在 launchd 跑（pid 通过 launchctl print 看）
# - bot 环境有 CLAUDED_TESTBOT_ID=1503327917550342235
# - .testbot.env.txt 有 token

PYTHONPATH=src .venv/bin/python scripts/e2e/run_real_e2e.py
```

输出：`data/e2e-reports/real-YYYY-MM-DD_HHMMSS.md`。

### 当前结果

**9 cases, 7 PASS / 0 FAIL / 2 SKIP**：

| # | Case | 时长 | 验证什么 |
|---|---|---:|---|
| 1 | M1 @mention starts session | 12s | bot 在 #testbot 收到 @mention → 创建 thread → claude 回复，bot.id 在 thread author 中 |
| 2 | Probe exact-text | 9s | bot 一字不差返 "探针 OK"（claude 真跑） |
| 3 | **#242 4K image preprocess** | 65s | testbot 发 3840×2160 PNG → grep bot.log 找 `#242: preprocessed probe.png: 3840x2160 (74937) -> 1900x1069 (65725)` ← **生产日志真实验证** |
| 4 | Long text streaming | 17s | claude 输出 293 字诗 → bot 分 2 条消息典型 typewriter 模式 |
| 5 | **Markdown table → PNG** | 63s | claude 输出 markdown 表格 → bot reply 含 `.png` attachment（table_png 路径生效） |
| 6 | **M6 3rd-party silent** | 33s | testbot 自己创 thread + 普通消息 → bot **不响应** (correct) |
| 7 | **M6b 3rd-party with mention** | 12s | 同上 + @mention → bot **响应** (correct) |
| 8 | /log dump | — | ⏭ testbot 是 bot account，没法 invoke slash on behalf of user |
| 9 | Unbound refuse | — | ⏭ 需要独立 never-bound 测试 channel |

### 顺带验证

测过程中观察到 bot 真返回 footer:
```
-# 💰 $0.1408 │ 📥 28.1k │ 📤 8 │ ⏱️ 6.2s │ 🧠 2.8%
-# ⚡ bypassPermissions
```
→ 实证 **PR #243 (per-turn cost)** + **PR #244 (🧠 精度)** 在生产真生效。

### 覆盖什么 mock harness 不能

| 维度 | Mock | Real |
|---|:---:|:---:|
| Cog callback 逻辑 | ✓ | (隐式) |
| Project / Session / Cost / Agent state 读写 | ✓ | (隐式) |
| Discord HTTP 上传 attachment | ✗ | ✓ |
| Discord gateway dispatch (`on_message`) | ✗ | ✓ |
| Discord thread 创建 | ✗ | ✓ |
| `bot.id` 收到的真消息 + 真用户上下文 | ✗ | ✓ |
| Claude CLI 子进程实际行为 | 部分 | ✓ |
| typewriter 真渲染 (Message.edit) | ✗ | ✓ |
| 真 thread.parent_id walks (#197) | 部分 | ✓ |
| bot 真 log file 落盘 | ✗ | ✓ (grep) |

### 限制

- **手动跑**：CI 不集成。原因：依赖 launchd-managed live bot + 网络 + Discord 速率限制 — 不 deterministic。
- **TesterBot 是 bot account**，**不能 invoke slash command**（Discord 限制 — bot 只能 invoke 自己定义的 slash）。`/log dump` 等纯 slash 命令只能靠 mock harness 覆盖。
- **测试期间生产 bot 实际在响应**：所有 7 个真测试都在生产 bot 上跑过 — 不在 sandbox。`#testbot` channel 临时绑定 `/tmp/img-probe`，测完恢复。

---

## 4. 诊断 / Probe 脚本

ad-hoc 验证脚本，用于：
- 复现某个 bug
- 验证某次 fix 工作

### `scripts/selftest.py` — bot smoke test
对 bot 健康做基本 ping。

### `scripts/e2e_truncation.py` — #107/#113 截断 bug 专项
跑实际 Claude SDK 长输出 + Discord 真发送，验证 typewriter 不截断。两种 mode：
- `--mode A` happy path
- `--mode B` 注入 HTTP 503 验证 retry

### `scripts/repro_truncation.py` — 单一截断现场离线复现

### ad-hoc Python one-liners
session 内大量使用，例如：

```python
# 复现 #252 CostTracker race
.venv/bin/python -c "
import asyncio, sys; sys.path.insert(0, 'src')
from clauded.cost_tracker import CostTracker
...
"
```

→ 通常用来：(a) 探 SDK 实际返回 schema (b) 验证 fix 工作 (c) 把行为打印出来给用户看。

---

## 测试结果总结表（截至 5/19）

| 类型 | Pass | Fail/XFail | Error | Skip |
|---|---:|---:|---:|---:|
| Unit tests | 1071 | 17 (open bugs) | 0 | 0 |
| Mock e2e | 101 | 17 (same open bugs) | 0 | 0 |
| Real Discord e2e | 7 | 0 | 0 | 2 |

**实际 PASS rate**：
- Unit: **98.4%** (1071 / 1088)
- Mock e2e: **88.1%** (101 / 118)
- Real e2e: **100%** of attempted (7 / 7)
- 17 XFAIL 都是已开 issue 的反向 reproducer，**fix PR 合并 → 自动 PASS** → 用 `git bisect`-like 方式定位回归。

---

## 已知未修 Bug（XFAIL 在追踪）

| Issue | What | 反向 reproducer case |
|---|---|---|
| #247 | KNOWN_MODELS 表过期 | `case_247_model_list_known_models_freshness` |
| #248 | cost record gated `> 0` 丢 0-cost turn | `case_248_cost_record_skip_zero` |
| #252 | `_save()` race condition 跨 4 个 store | `case_252_cost_tracker_race` + concurrent suite |
| #254 | `/agent create` / `/mcp add` silent overwrite | `case_254_agent_create_duplicate_silently_overwrites` |
| #255 | 5 处 input validation 缺失 | 4 个 `case_255_*` |
| #257 | 9 个 cog 漏 `reject_if_unbound` | 6 个 `case_257_*` |

---

## 测试覆盖盲点

诚实记录哪些场景**测不到**：

1. **launchd 行为 (#232 round 3)**: macOS-specific resource accounting; 只能靠 60+ min spot-check `log show --predicate ... clauded` 查 `because inefficient`。
2. **多用户并发 thread**: testbot 只有一个账号，无法模拟 N 个用户同时发消息打 bot。
3. **网络故障注入** in real e2e: 没有 inline-proxy 截 Discord HTTP 注 503/超时。Mock harness 部分覆盖了。
4. **Discord 速率限制行为**: 跑 real e2e 时如果碰到 429，会假性 FAIL。当前手动跑频率低，不命中。
5. **discord.py 内部 race (interaction expiry, defer 超时)**: 没系统测过。
6. **跨 bot 重启状态保留**: 实际 PASS 过 `case_session_state_resume_after_restart`，但只测了 SessionStore 直接落盘 + 重载，没测 launchd-kill-mid-turn 这种半完成状态。

---

## 怎么扩展测试

### 加 unit test

```python
# tests/test_<module>.py
import pytest
from clauded.<module> import <thing>

def test_<scenario>():
    # 真的验证状态 / 输出，不是只验非空
    assert ...
```

### 加 mock e2e case

```python
# scripts/e2e/run_e2e.py — 在文件末尾 HAPPY_CASES.extend(...) 之前加

async def case_my_new_test(bot) -> CaseResult:
    """对什么的什么 case."""
    from clauded.cogs.X import ...
    inter = make_mock_interaction(bot=bot)
    # bind 如果命令需要
    bot.project_manager.bind(inter.channel_id, str(ROOT))
    await cb(inter, ...)
    reply = _interaction_response_text(inter)
    # 严格断言：检查具体字符串 / 状态
    if "<expected substring>" in reply and bot.X.<state>:
        return CaseResult(cog="X", cmd="Y", case="Z", status="PASS", detail=...)
    return CaseResult(..., status="FAIL", detail=...)

HAPPY_CASES.extend([
    ("/X Y Z my-test", case_my_new_test),
])
```

会自动被 `tests/test_e2e_suite.py` pickup 进 pytest。

### 加 real Discord e2e case

```python
# scripts/e2e/run_real_e2e.py

async def case_my_real_test(driver: TestBotDriver) -> CaseResult:
    msg = await driver.post(f"<@{BOT_USER_ID}> ... 触发场景")
    replies = await driver.wait_for_bot_reply(
        msg, timeout=TIMEOUT_S, match="<期待字符串>"
    )
    # 验证 replies 内容、attachment、thread 创建等
    ...

# 加到 cases list 里
```

---

## CI 状态

`pytest tests/` 跑 **1071 + 17 xfail** 全套（含 mock e2e）—— 已经是 PR 检查的一部分。

`run_real_e2e.py` **不自动跑** — 仅在以下时刻手动：
- 大改 bot 行为 (cog / on_message / 渲染) 前后
- 验证 PR 在 prod 真生效（"#242 fix 真的工作吗？"）
- bug repro

---

## 历史里程碑

- 2026-05-13 起步：~318 unit tests
- 2026-05-18 中期：789 unit tests（v1.18 PR 序列 #114-#220）
- 2026-05-18 晚：946 unit tests（#221-#246）
- 2026-05-19 起 e2e harness：+40 → +73 → +118 mock cases，+9 real cases
- 2026-05-19 末：**1071 unit + 17 xfail + 9 real e2e** （当前）

---

## 文件索引

```
docs/
├── e2e/
│   └── command-inventory.md         # 自动生成的 69 cog 命令清单
└── prd/
    ├── v1.18-e2e-full-coverage.md   # #251 epic PRD
    ├── v1.18-image-preprocess.md    # #242 PRD
    └── ...

scripts/e2e/
├── run_e2e.py                       # Mock harness (Approach D) — 118 cases
└── run_real_e2e.py                  # Real Discord harness (Approach E) — 9 cases

tests/
├── conftest.py                      # 共享 fixtures
├── test_e2e_suite.py                # 将 mock harness 适配到 pytest
└── test_*.py (73 个)                # Unit tests

data/e2e-reports/                    # 测试运行报告 (gitignored)
├── YYYY-MM-DD_HHMMSS.md             # Mock e2e 报告
└── real-YYYY-MM-DD_HHMMSS.md        # Real e2e 报告
```

---

## 一句话总结

**Unit tests** 验证模块契约（最广，最快）。**Mock e2e** 验证 cog 全链路 + 状态变化（中速，无 Discord 但严格断言）。**Real Discord e2e** 验证生产 bot 真在 Discord 里能用（慢、手动、无可替代地真实）。三层互补，**没有任何一层 PASS 等于"真验证"** — 三层都有 PASS 才算。
