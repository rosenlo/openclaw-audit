# OpenClaw Context Overflow 调查报告

**调查日期:** 2026-06-19
**调查环境:** OpenClaw gateway 跑在 rosen@172.27.15.62(launchd: ai.openclaw.gateway.plist, port 18789)
**触发场景:** 通过 Telegram bot 下发任务,任务执行中被 context overflow recovery 打断,体感为「被中断」
**调查范围:** openclaw dist(`/opt/homebrew/lib/node_modules/openclaw/dist/`)+ 运行时日志(`~/Library/Logs/openclaw/gateway.log`)+ session trajectory(`~/.openclaw/agents/main/sessions/`)

---

## TL;DR

1. **OpenClaw 有两条独立的 context 管理线,不要混淆:**
   - **自动 compact**(`runPreflightCompactionIfNeeded`):turn 之间触发,阈值 `contextWindow - reserveTokensFloor(25000) - softThreshold(4000)` ≈ **233k tokens**(agnes-flash)。或 transcript 字节 ≥ `maxActiveTranscriptBytes`(**10MB**)。
   - **precheck overflow**(`shouldPreemptivelyCompactBeforePrompt`):**每次发 prompt 前**触发,阈值 `contextWindow - reserveTokens(60000)` ≈ **202k tokens**(agnes-flash)。本调查的「📦 上下文溢出 subtype=precheck」就是这条。
2. **10MB 的 `maxActiveTranscriptBytes` 在本场景从未开火** —— prompt 在 202k token 时先撞 precheck,transcript 字节到不了 10MB。
3. **本机 19 次 precheck 全部走 `truncate_tool_results_only`** —— 没有触发被动 compact,**对话历史未被摘要丢弃**,任务指令连续性在;但每次砍掉 19~137 个 tool result,是「被中断」体感的真正来源。
4. **元凶是 tool result 堆积**:单次 prompt 里最多有 **19 万字符**的 tool result 可压缩(bash 输出、文件读取、grep 结果未截断)。
5. **治本方向:源头限流 tool result 大小 > 主动 compact > 调 reserve。**

---

## 1. 配置现状(`~/.openclaw/openclaw.json`)

### compaction 块
```json
"compaction": {
  "reserveTokens": 60000,
  "reserveTokensFloor": 25000,
  "truncateAfterCompaction": true,
  "maxActiveTranscriptBytes": 10000000
}
```

### 模型 contextWindow
| 模型 | provider | contextWindow | 真实窗口(provider 侧确认) |
|---|---|---|---|
| agnes-flash | litellm | 262144 | 256K |
| gpt-5.4-mini | openai (Codex) | 258000 | 258K |
| gpt-5.4 | openai (Codex) | 258000 | 258K |
| gpt-5.5 | openai (Codex) | 258000 | 258K |

**注意:** contextWindow 不能随意调大 —— provider 侧真实窗口是天花板,调大 config 会被 runtimeCap 压回,或请求在 provider 侧 400。

### 两个 reserve 的区别(容易混淆)
| config key | 值 | 用于哪条线 |
|---|---|---|
| `reserveTokens` | 60000 | **precheck overflow** 阈值(`shouldPreemptivelyCompactBeforePrompt`) |
| `reserveTokensFloor` | 25000 | **自动 compact** 阈值(`runPreflightCompactionIfNeeded`) |

**结论:** 调 `reserveTokensFloor`(25000→12000 之类)只影响自动 compact 那条线,对 precheck overflow **无效**。precheck 用的是 `reserveTokens: 60000`。

---

## 2. precheck overflow 触发机制

### 源码位置
- `dist/attempt.tool-run-context-CT5r1Qgk.js:126` — `shouldPreemptivelyCompactBeforePrompt(params)`
- `dist/attempt.tool-run-context-CT5r1Qgk.js:178` — 日志格式 `formatPrePromptPrecheckLog`
- `dist/embedded-agent-BgvyyCVT.js` — recovery 执行
- `dist/selection-kQiC501t.js:7379` — midturn precheck 调用点

### 判定逻辑(agnes-flash 实算)
```
contextTokenBudget        = 262144
minPromptBudget           = min(8000, 262144 * 0.5) = 8000
effectiveReserveTokens    = min(reserveTokens=60000, 262144 - 8000) = 60000
promptBudgetBeforeReserve = 262144 - 60000 = 202144
overflowTokens            = max(0, estimatedPromptTokens - 202144)
```

**overflow 触发条件:** 估算的 prompt token 数 > 202,144。

### recovery 路由(line 161-165)
```js
if (overflowTokens > 0)
  if (toolResultReducibleChars <= 0)        route = "compact_only";
  else if (toolResultReducibleChars >= truncateOnlyThresholdChars)
                                            route = "truncate_tool_results_only";
  else                                      route = "compact_then_truncate";
```

| route | 做什么 | 是否丢对话历史 |
|---|---|---|
| `fits` | 正常发 | 否 |
| `truncate_tool_results_only` | 只截断 tool results | **否** |
| `compact_then_truncate` | 先 compact 再截断 | 是(摘要) |
| `compact_only` | 强制 compact | 是(摘要) |

### 日志格式
```
[context-overflow-precheck] pre-prompt check sessionKey=... provider=litellm/agnes-flash
  route=... estimatedPromptTokens=... promptBudgetBeforeReserve=202144 overflowTokens=...
  toolResultReducibleChars=... reserveTokens=60000 effectiveReserveTokens=60000
  contextTokenBudget=262144 messages=NaN unwindowedMessages=NaN sessionFile=...
```

**`messages=NaN` 的原因:** precheck 阶段 `params.messageCount` 未传入(`Math.max(0, Math.floor(undefined))` → NaN)。非 bug,是该阶段信息缺失。`openclaw-audit.py` 此前因 `int("NaN")` 抛错把字段丢了显示 `msgs=?`,已在 PR #1 修复(显示 `n/a`)。

---

## 3. 实际日志分析(gateway.log,共 19 次 precheck)

### 数据汇总
```
所有 19 次 route = truncate_tool_results_only  (无一例外)
truncatedCount 范围:        19 ~ 137 条 tool result 被砍
toolResultReducibleChars:   36,134 ~ 191,973 字符可压缩
overflowTokens:             190 ~ 12,954
estimatedPromptTokens:      108,345 ~ 206,523 (截断后值)
```

### 最近两次(最相关)
```
2026-06-19T20:58:50  route=truncate_tool_results_only  truncatedCount=70
                     estimatedPromptTokens=202334  overflowTokens=190
                     toolResultReducibleChars=167559
                     sessionFile=.../001df004-....jsonl

2026-06-19T23:01:51  route=truncate_tool_results_only  truncatedCount=48
                     estimatedPromptTokens=206523  overflowTokens=4379
                     toolResultReducibleChars=191973
                     sessionFile=.../09e6acae-....jsonl
```

### 关键结论
1. **没有一次走 compact** — 对话历史/任务指令从未被摘要丢弃。「被中断」不是因为丢了上下文,而是 tool result 被裁。
2. **tool result 堆积是元凶** — 单次 prompt 最多 19 万字符的 tool result 可压缩,这是 bash/读文件/grep 输出未截断在 transcript 里堆出来的。
3. **estimatedPromptTokens 与 overflowTokens 口径不一致** — 日志里 est 是截断后的值,overflow 是截断前算的。两个数字不能直接相加推断触发时 prompt 大小,但**不影响结论**:overflow 确实在触发,靠砍 tool result 救。

---

## 4. 对 Telegram bot 任务的影响

| 影响项 | 说明 |
|---|---|
| **延迟** | 每次 overflow recovery = 检测→截断→重发 prompt 一个额外 round-trip,该轮回复明显变慢 |
| **正确性风险** | 砍掉 48-137 个 tool result,LLM 可能丢掉当前任务需要的上下文细节(grep 某行、文件某段),导致瞎猜或重跑 tool |
| **任务指令连续性** | 未受影响(没走 compact),你下发的任务指令和讨论都在 |
| **触发集中** | 最近两次在 20:58 和 23:01,对应那两次 Telegram 任务应该都卡了一下 |

**「被中断」体感的根因:** LLM 正需要的 tool result 被 precheck 兜底截断 → 基于残缺信息继续 → 要么跑偏要么重跑 → 你感觉任务被打断。

---

## 5. 建议(按 ROI 排)

### 5.1 源头限流 tool result(最直接,治本) ✅ 已确认

**配置路径:** `agents.defaults.contextLimits.toolResultMaxChars`

**默认值计算逻辑(源码 `tool-result-truncation-CE7-U3RC.js`):**

| 模型 contextWindow(tokens) | 默认 toolResultMaxChars | 说明 |
|---|---|---|
| < 100,000 | **16,000 字符** | 默认值 DEFAULT_MAX_LIVE_TOOL_RESULT_CHARS |
| 100,000 ~ 200,000 | **32,000 字符** | LARGE_CONTEXT_MAX_LIVE_TOOL_RESULT_CHARS |
| ≥ 200,000 | **64,000 字符** | XL_CONTEXT_MAX_LIVE_TOOL_RESULT_CHARS |

agnes-flash 的 contextWindow=262144 ≥ 200k,所以**自动 cap 是 64,000 字符/tool result**。

此外还有一个**硬上限**计算: `min(contextWindowTokens * 0.3 * 4, hardCap)` = `min(262144 * 1.2, 64000)` = `min(314572, 64000)` = **64,000 字符**。

**实测对比:** 日志显示单次 `toolResultReducibleChars` 高达 191,973 字符,远超 64k 单条 cap。这不意味着"单条 tool result 塞进 100k+"—— 单条注入时已被 64k cap 截断。19 万可压缩量来自**聚合预算**:

**聚合预算(aggregate budget)** — 源码 `calculateRecoveryAggregateToolResultChars`(`tool-result-truncation-CE7-U3RC.js:235`):
```js
return Math.max(1, aggregateMaxCharsOverride ?? maxCharsOverride ?? calculateMaxToolResultChars(contextWindowTokens));
```
未配 `aggregateMaxCharsOverride` 时,**聚合预算 fallback 到单条 cap = 64k**。即默认配置下"所有 tool result 加起来不能超 64k 字符"。但你 transcript 里几十条 tool result × 每条接近 64k = 总量几十万,远超 64k 聚合预算 —— 超出部分全是 precheck recovery 时的"可压缩量"。

**这解释了为什么 precheck 一次砍 48~137 条 tool result**(`truncatedCount`):recovery 时要把总量从几十万压回 64k 聚合预算内,得砍一大批。

**两个独立旋钮(校准:聚合预算 = 单条 cap × 4,不是 fallback 到单条值):**
| 旋钮 | 默认(agnes-flash) | 作用 |
|---|---|---|
| `toolResultMaxChars`(单条 cap) | 64,000 字符/条 | 注入时截断单条 |
| 聚合预算(aggregate budget) | **256,000 字符**(单条 cap × 4) | recovery 时砍到的总量目标 |

**聚合预算的倍数关系:** 源码 `calculateRecoveryAggregateToolResultChars` + `PROMPT_TOOL_RESULT_AGGREGATE_CAP_MULTIPLIER = 4`。聚合预算 = 单条 cap × 4,**不是** fallback 到单条值。agnes-flash 默认 64k × 4 = **256k 总量**。这修正了上文"压回 64k 聚合预算"的说法 —— 实际 recovery 时是把总量压回 **256k**(旧默认)。

`aggregateMaxCharsOverride` 可单独配聚合预算(不配则 = 单条 cap × 4)。

**两条路径,config 生效范围不同(重要):**
| 路径 | 读 config? | 用什么 cap |
|---|---|---|
| `resolveLiveToolResultMaxChars` | ✅ 读 `agents.defaults.contextLimits.toolResultMaxChars` | precheck recovery、compact successor transcript 写入 |
| `calculateMaxToolResultChars(contextWindowTokens)` | ❌ 不读 config,自动按 contextWindow 分档 | `truncateOversizedToolResultsInMessages` / `truncateOversizedToolResultsInSession` 等部分路径 |

**含义:** `toolResultMaxChars` 配置**只在走 `resolveLiveToolResultMaxChars` 的路径生效**(precheck recovery 主路径、compact 写入)。某些直接调 `calculateMaxToolResultChars` 的路径仍按自动值(agnes-flash 64k)。主路径生效即可,但不要假设"所有路径都变 16k"。

配小 `toolResultMaxChars` 从源头减小每条 tool result 进 transcript 的大小 → 总量增长变慢 → 更晚撞 202k precheck。**对累积型 overflow 有实质帮助**(每条更小 = 总量更小)。但注意聚合预算也同比缩小(64k × 4 = 256k → 16k × 4 = 64k),recovery 目标更低,一旦触发砍得更狠 —— 涨得慢 vs 砍得狠,部分抵消。真正降触发频率仍需配合阶段性 compact 清存量。

**配置方法:**
```json5
// ~/.openclaw/openclaw.json
{
  "agents": {
    "defaults": {
      "contextLimits": {
        "toolResultMaxChars": 16000  // 配到 16k,与 256k 以下模型档位一致
      }
    }
  }
}
```

配到 16k 后,每个 tool result 最多保留 16k 字符,19 次 precheck 中大部分应该不会再触发。配合 precheck 的 `truncate_tool_results_only` 路由,效果是**主动限流 + 被动兜底**双重保障。

**注意:** 源码限制 `toolResultMaxChars` 范围 1 ~ 250,000(整数)。配太小(如 1000)可能丢失有用上下文,建议从 16k 起步,根据 `/context detail` 的实际效果微调。

### 5.2 主动 compact — 阶段性完成时压,不是任意时机压

**核心原则:在任务阶段性完成时 compact,不要在思路正展开时压。** compact 摘要质量强依赖于"被压缩内容是否是一个完整、可总结的单元"。

#### compact 的失忆风险(两层)

**第一层:不可避免的摘要损失(轻度)**
compact 是有损压缩 —— 几千字对话 + 几十条 tool result 压成几百字摘要。必然丢失:
- 具体数值/代码细节(grep 某行、文件某段、test 输出)
- 过程中的失败尝试(试过 A 不行改用 B,摘要往往只留 B)
- tool result 原文(摘要只说"读过 X,关于 Y",不留原文)

这种损失无法避免,但 LLM 仍"知道"做过什么 —— **大方向不失忆,细节失忆**。

**第二层:危险的失忆 — 摘要漏掉了当前任务还需要的东西**
真正坑你的场景:compact 时没保留某个当前任务还需要的细节,后续 LLM 基于不完整记忆跑偏或重做。例如改 bug 时前面确认"根因在 line 42 off-by-one",若 compact 没保留这句,LLM 可能重新找根因白费几轮。

#### 时机选择(决定失忆风险高低)

| 时机 | 摘要质量 | 失忆风险 |
|---|---|---|
| **阶段性完成时**(子任务做完、自然停顿) | 高 — 完整单元,能准确概括 | 低 |
| 思路正展开时(刚读一堆文件、还没综合) | 低 — 半截状态,易漏"在用但没结论"的细节 | 高 |
| 撞 202k 被动触发(precheck 自动压) | 最低 — 时机不受控,可能压在最需要细节时 | 最高 |

**好时机:** 子功能改完 test 过了 / 一轮调研结束结论清楚了 / 自然停顿点
**坏时机:** 刚读完文件还没综合 / 正在调试错误没复现清楚 / 即将做的事依赖前面具体细节

#### 怎么压 — 用指令控制保留什么(降低失忆的关键)

主动 compact 比被动 precheck 最大的优势:**能传 `/compact 重点保留 <要点>` 指令**,明确哪些必须留。不传指令则 LLM 自己决定,容易漏。

```
/compact 重点保留:已确认 root cause 在 parser.py:42 off-by-one;fix A 失败原因;当前采用 fix B;下一步改 test_parser.py
```

#### /compact vs /new — 高频 compact,极少 new

| | `/compact` | `/new` |
|---|---|---|
| 做什么 | 老消息摘要保留,session 继续 | 旧对话历史整体丢弃,从零开始 |
| 记忆 | LLM 还记得之前讨论过什么(摘要) | **完全失忆**,前文全没 |
| sessionKey | 不变 | 变(新 session) |
| 适用 | 上下文吃紧但任务没完 | 彻底换话题、上下文已污染 |
| Telegram 任务中 | ✅ 任务边界/阶段完成用它 | ❌ 任务中途用 = 前面白干 |

**`/new` = 丢弃记忆**(走 `runSessionResetFromAgent({reason:"new"})`,旧 transcript 不再引用)。任务没完就 `/new`,LLM 对之前讨论、读过的文件、跑过的命令全部失忆。**日常高频用 `/compact`,极少用 `/new`**(只在彻底换不相关新任务时)。

#### 失忆兜底
即使 compact 漏了细节,session transcript 原文件还在(`~/.openclaw/agents/main/sessions/*.jsonl`),原始对话没删,只是 LLM 上下文里没有。需要时可让 LLM 重读那段 transcript,或自己 grep。`/context detail` 能看当前保留了什么。

#### compact / new 对 toolResultReducibleChars 的影响

`toolResultReducibleChars` 是 precheck 时算的"当前 transcript 里所有 tool result 超出聚合预算(默认 64k 总量)的部分",即还能砍多少。减少它 = transcript 里 tool result 总量降下来了。

**`/compact` — 显著降,但是临时性的**
源码确认(`compaction-successor-transcript-Ncp4Uf5J.js:362-363` 注释):compact 时**老消息(含其 tool result)整体被 compaction summary 摘要文本替换** —— 这些老 tool result 不再是 toolResult 类型消息,变成摘要里的普通文本,**不再计入 `toolResultReducibleChars`**。近期未压缩消息里的 tool result 保留,仍按 64k 单条 cap(`resolveLiveToolResultMaxChars`)。

- 效果:累积的几十条老 tool result 被摘要替代,`toolResultReducibleChars` 显著下降。但**不会归零** — 近期消息里的 tool result 仍可能超 64k 聚合预算。
- **关键限制:compact 是周期性清零老存量,不是一劳永逸。** compact 完继续跑 bash/grep,新 tool result 会再累积,`toolResultReducibleChars` 很快涨回来。

**`/new` — 接近归零,但丢全部记忆**
新 session 无历史 tool result,`toolResultReducibleChars` 基本归零(只有第一轮的 tool result)。最彻底,但代价是完全失忆(见上表),任务中途不能用。

**`toolResultMaxChars` 配小 — 不直接降,但减缓增长速度**
控制单条 tool result 进 transcript 时多大(注入时限流)。不直接降 `toolResultReducibleChars`,但每条更小 → 总量涨得更慢 → compact 之间能撑更久。

**三者关系:限流 + 定期清理,不是替代。**
| 手段 | 作用层面 | 效果 |
|---|---|---|
| `toolResultMaxChars` 配小 | 注入时限流单条 | 减缓累积速度(治标,持续) |
| `/compact` 阶段性 | 清掉老存量 | 直接降存量(临时,会再涨) |
| `/new` | 清空全部 | 归零(彻底,丢记忆) |

**容易误解的点:** compact 把 tool result 摘掉了 ≠ 不用配 `toolResultMaxChars`。两者作用层面不同 —— `toolResultMaxChars` 让每条更小(涨得慢),compact 定期清老的(降下来)。**配小 + 阶段性 compact 两个一起,才能让 `toolResultReducibleChars` 长期保持低位、少触发 precheck。**

**对 Telegram 任务的实操含义:**
1. 阶段性完成时 `/compact 重点保留 <要点>` — 清这一阶段的 tool result 存量,直接降 `toolResultReducibleChars`
2. 配小 `toolResultMaxChars`(16k)— 让每条 tool result 涨得慢,compact 之间撑更久
3. `/new` 只在彻底换话题 — 降得最彻底但丢记忆,任务中途禁用

注意 compact 是周期清零不是永久解决:compact 完继续跑 tool 还是会涨,所以必须配合 `toolResultMaxChars` 限流 + 阶段性 compact 才稳。

- 主动压比撞 202k 被动压质量高:可传指令控制摘要保留什么,且在阶段性完成时压(不是任意时机)。
- `reserveTokensFloor` 调整对 precheck 帮助不大(precheck 用 60k reserve,不是 25k 那条)。

### 5.3 评估缩小 reserveTokens(谨慎)
precheck 用 `reserveTokens: 60000` 给 LLM 回复留 60k。若 Telegram 任务回复普遍不长,60k 偏大,缩到 30k-40k 可把 promptBudget 从 202k 拉到 ~222k,少触发一些 overflow。但只是推迟,不治本,且压缩回复空间有风险。

### 5.4 不建议的方向
- **调大 contextWindow** — provider 侧 256k/258k 是天花板,无效。
- **调大 maxActiveTranscriptBytes** — 该线在本场景从未开火,与本问题无关。
- **开 session-logs** — 对排查有帮助(留 verbose 日志),但不解决问题本身。

---

## 6. 相关文件索引(远端 62)

| 路径 | 说明 |
|---|---|
| `~/.openclaw/openclaw.json` | 主配置(compaction 块、模型 contextWindow) |
| `~/Library/Logs/openclaw/gateway.log` | gateway 运行时日志(launchd 重定向 stdout 到此,stderr → /dev/null) |
| `~/Library/LaunchAgents/ai.openclaw.gateway.plist` | launchd 配置,含日志路径 |
| `~/.openclaw/agents/main/sessions/*.trajectory.jsonl` | session trajectory(事件流) |
| `~/.openclaw/state/openclaw.sqlite` | 状态库(sessions/diagnostic_events 等) |
| `~/.openclaw/logs/` | config-audit / gateway-restart / stability 日志(非运行时 verbose) |
| `/opt/homebrew/lib/node_modules/openclaw/dist/attempt.tool-run-context-CT5r1Qgk.js` | precheck 核心逻辑 |
| `/opt/homebrew/lib/node_modules/openclaw/dist/agent-runner.runtime-BapylDFW.js` | 自动 compact 逻辑(runPreflightCompactionIfNeeded) |
| `/opt/homebrew/lib/node_modules/openclaw/dist/context-resolution-DvriSJiG.js` | contextWindow 解析(含 runtimeCap 限制) |
| `/Users/rosen/workspace/openclaw-audit/openclaw-audit.py` | 审计脚本(PR #1 已修 precheck 字段解析) |

---

## 7. 本次调查已完成的修复

**openclaw-audit.py PR #1(已 merge):** 修复 `msgs=?` 显示问题。
- 根因:OpenClaw 日志输出 `messages=NaN`,脚本 `int("NaN")` 抛错丢字段。
- 修复:加 `_parse_int_field` helper 返回 None;渲染显示 `n/a`;顺带解析 `estimatedPromptTokens` / `overflowTokens` 字段(此前被丢弃)。
- 仓库:https://github.com/rosenlo/openclaw-audit

---

## 7.5 配置落地验证(2026-06-19 实测)

在 `~/.openclaw/openclaw.json` 配置 `agents.defaults.contextLimits.toolResultMaxChars: 16000`,重启 gateway 后日志确认生效:

```
[tool-result-truncation] Truncated 42 tool result(s) for prompt history (maxChars=16000 aggregateBudgetChars=64000)
```

**关键数据对比:**
| 指标 | 旧值(默认) | 新值(配 16k) | 变化 |
|---|---|---|---|
| 单条 tool result cap (`maxChars`) | 64,000 | 16,000 | ↓75% |
| 聚合预算 (`aggregateBudgetChars`,cap × 4) | 256,000 | 64,000 | ↓75% |

**确认的机制点:**
1. **聚合预算 = 单条 cap × 4**(`PROMPT_TOOL_RESULT_AGGREGATE_CAP_MULTIPLIER`)。日志 `aggregateBudgetChars=64000` = 16000 × 4,正常,非 bug。旧默认 256000 = 64000 × 4。
2. **重启时回溯截断**:gateway 重启后用新 16k cap 对已存在的旧 transcript(按 64k 存的)做了一次主动截断(42 条)。这是配置生效时顺带清理历史存量,**只发生一次**,后续新 tool result 按 16k 进,不再需要回溯截断。
3. **配置生效路径**:`maxChars=16000` 出现在日志里,确认 precheck recovery 主路径(`resolveLiveToolResultMaxChars`)读到了 config。注意部分路径(`calculateMaxToolResultChars`)不读 config 仍用自动 64k(见 5.1 两路径区分)。

**预期校准(保守):**
配 16k 后每条 tool result 更小 → 总量涨得慢 → 推迟触发。但聚合预算也从 256k 降到 64k(recovery 目标更低),一旦触发砍得更狠。两效应部分抵消,所以**净效果是"触发可能稍晚、但一旦触发砍得更狠"**,不一定是"触发次数大幅下降"。真正降频率仍需阶段性 compact 配合。待长任务观察新配置下 precheck 实际触发数据后补充。

---

## 8. 待办 / 下一步

- [x] 确认 OpenClaw `toolResultMaxChars` 配置项路径与默认值,在 openclaw.json 里配小(5.1)
- [ ] 在 openclaw.json 写入 `agents.defaults.contextLimits.toolResultMaxChars: 16000` 并重启 gateway
- [ ] 跑一次 `/context detail` 在长 Telegram session 里,看哪类 tool result 占 token 最多(验证 16k cap 是否够用)
- [ ] 养成任务边界主动 `/compact` 习惯(5.2)
- [ ] 如 overflow 仍频繁,再评估缩小 `reserveTokens` 60000→40000(5.3)
