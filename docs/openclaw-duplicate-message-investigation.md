# OpenClaw 重复消息调查报告

**调查日期:** 2026-06-21
**调查环境:** OpenClaw gateway 跑在 rosen@172.27.15.62(launchd: ai.openclaw.gateway.plist, port 18789), 已安装版本 `openclaw@2026.6.8`
**触发场景:** Telegram bot 偶发把同一条消息发两遍(最近一次: 2026-06-21 00:45 (+07) 前后, "好,先 spawn Architect 设计 #19。")
**调查范围:** 运行时日志(`/tmp/openclaw/openclaw-*.log`)+ session transcript(`~/.openclaw/agents/main/sessions/*.jsonl`)+ openclaw dist 源码(`/opt/homebrew/lib/node_modules/openclaw/dist/`)+ 上游仓库(`github.com/openclaw/openclaw`)

---

## TL;DR

1. **重复不是偶发, 是一个确定性的状态机泄漏。** 2026-06-19 和 2026-06-20 每天各触发 4 次 `failed to mirror outbound delivery into session transcript` WARN。
2. **根因链:** compaction rotation 重命名 transcript 文件 → outbound delivery 镜像写入时 fingerprint(inode) 校验失败 → 抛 `EmbeddedAttemptSessionTakeoverError` → delivery 状态卡在 `send_attempt_started` → 下次 reconnect drain 把它当"没发成功"重放 → 用户收到第二条。
3. **和 [context-overflow 调查](./openclaw-context-overflow-investigation.md) 相关但不是同一条线:** 两份调查共享 session transcript + compaction 子系统, 但那份是"上下文太大怎么裁 tool result"(读 transcript), 这份是"compaction rotation 改文件名撞坏了并发写入"(写 transcript)。
4. **升级解决不了。** npm latest = `2026.6.8` 就是本地版本。真正针对本场景的修复(PR #92274, "subagent announce 3x duplicate")**仍未合并**, 连 alpha 都没进。已合并的相关 PR(#89812、#90775)已在本地版本里但没堵住所有路径。
5. **治本方向:** mirror 失败时重试并重新解析 successor 文件路径; 或 delivery 状态推进与 transcript mirror 解耦, 拿到 Telegram messageId ACK 就标 delivered, 不依赖 mirror 成功。

---

## 1. 现象与日志定位

### 1.1 日志布局

- 日志按本地日期分文件: `/tmp/openclaw/openclaw-YYYY-MM-DD.log`, 时区 `+07:00`。
- 每行是一条 JSON, 关键字段: `time`(ISO +07:00)、`message`、`_meta.logLevelName`、`session_id`、`traceId`。
- 注意: `_meta.logLevelName` 才是真实 level, 顶层无 level 字段(openclaw-audit PR #7 已修这个解析)。

### 1.2 重复消息的两条来源(都在日志里亲眼看到)

**来源 A — reconnect drain 真重放了(用户收到重复)**, 2026-06-19 16:43:50:
```
16:43:50.351  [telegram][diag] Telegram reconnect drain: 1 pending message(s) matched telegram:default
16:43:50.356  [telegram][diag] Telegram reconnect drain: entry d2c9370e-... is already being recovered
16:43:50.732  telegram outbound send ok  messageId=1286  operation=sendMessage
16:43:51.318  telegram outbound send ok  messageId=1287
16:43:52.383  telegram outbound send ok  messageId=1288
16:43:53.368  telegram outbound send ok  messageId=1289
16:43:53.388  [WARN] failed to mirror outbound delivery into session transcript; channel send already succeeded: session file changed ...
```

**来源 B — drain 试图重放被防护拦下(没产生重复)**, 2026-06-20 23:02:04:
```
23:02:04.688  [telegram][diag] Telegram reconnect drain: 3 pending message(s) matched telegram:default
23:02:04.693  [telegram] Delivery entry 24c22059-... delivery state is send_attempt_started; refusing blind replay without adapter reconciliation
23:02:04.697  [telegram] Telegram reconnect drain: retry failed for entry 24c22059-...: delivery state is send_attempt_started
(另外两个 entry 同样被拦)
```

**来源 C — sendRichMessage 失败后自动重试(也产生重复)**, 2026-06-20 23:01:19:
```
23:01:19.847  [ERROR] telegram richMessage failed: ... RICH_MESSAGE_URL_I... (400)
23:01:19.874  Subagent completion direct announce failed for run 93e59404...
23:01:21.397  [ERROR] telegram richMessage failed: ...   ← 重试 1
23:01:23.943  [ERROR] telegram richMessage failed: ...   ← 重试 2
23:01:24.053  Subagent announce give up (retry-limit)
```
经典的 at-least-once 投递 + 非幂等 send + 响应丢失。其中某次其实已送达 Telegram, 但响应被当 400, 重试再发一遍。

### 1.3 messageId 单调递增、无重复

openclaw 侧每条消息只调一次 Telegram API, messageId(1665→1955)严格递增, 没有同 messageId 发两次。**重复发生在 Telegram 端**: 同一内容、不同 messageId 各发一次。这把责任指向"openclaw 认为没发成功于是重发", 而不是"Telegram API 收到一次发两次"。

---

## 2. 根因机制(从日志到源码)

### 2.1 出站投递的两段式设计

OpenClaw 的 Telegram 出站投递分两段:

1. **发送**: 调 Telegram API → 成功后把这次 delivery **镜像写进 session transcript(`.jsonl`)**, 并把 pending 队列里这条 entry 的状态从 `send_attempt_started` 推进到 `delivered`。
2. **reconnect drain**: 长轮询连接断线重连时, 扫描 pending 队列, 凡是还停在 `send_attempt_started`(即"调用已发出、没确认交付")的条目, 当作"没发成功"重新投递。

### 2.2 镜像写入的调用链(dist 源码位置)

```
dist/deliver-Cgr2aBSp.js:1393   if (params.mirror && results.length > 0)
deliver-Cgr2aBSp.js:1402         appendAssistantMessageToSessionTranscript({agentId, sessionKey, text, idempotencyKey, config})
                                 └─ dist/transcript-NdJkeRhp.js:927  appendAssistantMessageToSessionTranscript
                                     └─ transcript-NdJkeRhp.js:963  appendExactAssistantMessageToSessionTranscript
                                         └─ transcript-NdJkeRhp.js:1007  runWithOwnedSessionTranscriptWriteLock(...)
                                             └─ transcript-NdJkeRhp.js:573  runWithOwnedSessionTranscriptWriteContext
                                                 │  const ctx = ownedTranscriptWriteContext.getStore()
                                                 │  if (!ctx || !contextMatches(...)) return await run()   ← mirror 走这: 无 owned context
                                                 └─ transcript-NdJkeRhp.js:724  appendSessionTranscriptMessage
                                                     └─ withSessionTranscriptWriteLock → acquireSessionWriteLock
                                                         └─ dist/selection-kQiC501t.js:6225  fingerprint fence
                                                             if (sameSessionFileFingerprint(before, current)) return;
                                                             takeoverDetected = true;
                                                             throw new EmbeddedAttemptSessionTakeoverError(sessionFile);  ← line 6235
deliver-Cgr2aBSp.js:1405         if (!mirrorResult.ok) log.warn(`failed to mirror ... ${mirrorResult.reason}`, ...)
```

**关键点: mirror 发生在 outbound delivery 阶段, 此时 embedded run(子代理)已返回, prompt lock 释放, `ownedTranscriptWriteContext.getStore()` 返回空。** 于是 `runWithOwnedSessionTranscriptWriteContext` 走无锁分支 `return await run()`, 内部自己 `acquireSessionWriteLock`, 而这个锁带 fingerprint fence。

### 2.3 fingerprint 用 inode 做指纹

`selection-kQiC501t.js:6095` `readSessionFileFingerprint`:
```js
const stat = await fs.stat(sessionFile, {bigint: true});
return {exists: true, dev: stat.dev, ino: stat.ino, size: stat.size, mtimeNs: stat.mtimeNs, ctimeNs: stat.ctimeNs};
```

`selection-kQiC501t.js:6225-6236` 写前校验:
```js
if (sameSessionFileFingerprint(beforeWrite, current)) {
    // 文件没变 → 信任, 更新 fence
    fenceSnapshot = ...; fenceFingerprint = ...;
    return;
}
takeoverDetected = true;
throw new EmbeddedAttemptSessionTakeoverError(params.lockOptions.sessionFile);  // 文件变了就抛
```

### 2.4 为什么 inode 会变 — compaction rotation

mirror 失败的 session 文件名有两类:
- 带时间戳前缀: `2026-06-19T17-10-41-023Z_bfe56cc5-....jsonl`、`2026-06-20T16-36-38-474Z_54a9e12b-....jsonl`
- 普通 UUID: `594d6a20-f01f-4db4-a514-70a85fe2dd02.jsonl`

**带时间戳前缀的文件名正是 compaction successor transcript 的归档名**。对照日志里的 `rotated active transcript` 事件:

| MIRROR_FAIL 时间 | session 文件名时间戳(UTC) | 对应 +07 rotation 时刻 | 间隔 |
|---|---|---|---|
| 2026-06-20 00:24:42 | `2026-06-19T17-10-41Z` | 06-20 00:10:41 | 14 min |
| 2026-06-20 23:51:48 | `2026-06-20T16-36-38Z` | 06-20 23:36:38 | 15 min |
| 2026-06-20 22:52:28 | `594d6a20`(普通 UUID) | 900s 内无 rotation | — |
| 2026-06-20 23:24:59 | `594d6a20`(同上) | 102s 前有 `Turn transcript persistence failed ... session file changed` | — |

compaction rotation 把 `uuid.jsonl` **重命名**成 `<timestamp>_uuid.jsonl` 归档, 新写入走 successor 文件(新 inode)。mirror 拿着旧路径去写, fingerprint 的 inode 对不上 → 抛 takeover。

### 2.5 关键矛盾 — 幂等保护被 gating 在失败检查之后

`transcript-NdJkeRhp.js:746` `appendSessionTranscriptMessageLocked`:
```js
async function appendSessionTranscriptMessageLocked(params) {
    const idempotencyKey = readMessageIdempotencyKey(params.message);
    const existing = idempotencyKey && params.idempotencyLookup === "scan"
        ? await findTranscriptMessageByIdempotencyKey(params.transcriptPath, idempotencyKey) : void 0;
    if (existing) return {...existing, appended: false};   // ← 幂等短路: 已有同 key 消息就不重写
    ...
    await appendJsonlEntry(params.transcriptPath, entry);
}
```

设计上**有** idempotency 去重(`findTranscriptMessageByIdempotencyKey` 扫已有同 key 消息, `idempotencyLookup:"scan"`)。但它在这个 `*Locked` 函数里, **必须在持锁之后才执行**。而 fingerprint fence 在 `acquireSessionWriteLock` 拿锁阶段就抛了 → 永远走不到 idempotency 那行。**本该防住重复的幂等检查, 被卡在它前面的 inode 检查后面。**

### 2.6 mirror 失败只丢 transcript 记录, 不回滚 Telegram 发送

`deliver-Cgr2aBSp.js:1405`:
```js
if (!mirrorResult.ok) log.warn(`failed to mirror ... channel send already succeeded: ${mirrorResult.reason}`, {channel, to, sessionKey});
// 不抛错、不重试、不回滚 — 只记 WARN
```

消息已到 Telegram, 但 delivery entry 状态停在 `send_attempt_started`(因为推进到 `delivered` 的动作依赖 mirror 成功记账)。下次 reconnect drain 看到 `send_attempt_started` → 当"没发成功"重放 → **用户收到第二条**。

### 2.7 完整因果链

```
主 agent spawn 长时间子代理(Architect/Codex/Reviewer, agent.wait 140-811s), 持 embedded prompt lock
  ↓
子代理返回 → prompt lock 释放 → 主 agent 产出回复 → outbound send ok(Telegram 收到, messageId 分配)
  ↓
deliverer 镜像这次 delivery 进 transcript(appendAssistantMessageToSessionTranscript)
  ↓
mirror 跑在 owned-write context 之外 → 自己 acquireSessionWriteLock → fingerprint fence 校验
  ↓
lock 释放窗口里 compaction rotation 已把文件重命名 + 建 successor → inode 变 → fingerprint 不匹配
  ↓
throw EmbeddedAttemptSessionTakeoverError → mirror 返回 {ok:false} → log.warn(不抛错, 发送不回滚)
  ↓
delivery entry 状态停在 send_attempt_started(没推进到 delivered)
  ↓
下次 Telegram 长轮询 reconnect → drain 扫到 send_attempt_started 的条目 → 重放
  ↓
重放成功(06-19 16:43) → 用户收到重复 / 重放被 "refusing blind replay" 拦下(06-19 23:55、06-20 23:02)
```

---

## 3. 和 context-overflow 调查的关系

[那份调查](./openclaw-context-overflow-investigation.md) 调查的是 **precheck overflow → truncate tool results**(prompt 太大, 发 prompt 前裁 tool result)。两份调查共享 session transcript 文件 + compaction 机制, 但故障模式正交:

| | context-overflow 调查 | 本调查(重复消息) |
|---|---|---|
| 现象 | 任务执行中体感"被中断" | 一条消息在 Telegram 收到两遍 |
| 根因 | prompt 超 202k token → precheck 截断 tool result | compaction rotation 改 transcript inode → mirror 写入失败 → drain 重放 |
| 受影响环节 | context 管理(precheck/compact) | outbound/deliver + transcript 写入 |
| 方向 | **读** transcript 决定裁多少 | **写** transcript 时 inode 被换, 写入被拒 |
| 代码位置 | `attempt.tool-run-context-CT5r1Qgk.js`、`tool-result-truncation-CE7-U3RC.js` | `deliver-Cgr2aBSp.js`、`transcript-NdJkeRhp.js`、`selection-kQiC501t.js` |
| 修复方向 | 限流 `toolResultMaxChars` + 阶段性 `/compact` | 见第 5 节 |

**共享点:** 那份调查第 5.2 节提到的 `compaction-successor-transcript-Ncp4Uf5J.js` 正是本调查里 rotation 重命名的同一段代码。那份调查当时只关注它对 `toolResultReducibleChars` 的影响, 没注意到它对 outbound mirror 的副作用。

**precheck 不是本 bug 的触发者:** 对 4 次 MIRROR_FAIL 的前 600 秒窗口做事件对照, 没有一次 `[context-overflow-precheck]` 事件与之同期发生。precheck 的 `truncate_tool_results_only` 路由(那份调查的主角)和 mirror 失败时间上不重叠。

---

## 4. 上游修复状态(2026-06-21 核对)

本地版本 `openclaw@2026.6.8`(npm dist-tag `latest` 即此版本)。上游 `github.com/openclaw/openclaw` 相关 PR:

| PR | 合并时间 | 修复内容 | 在 2026.6.8? | 对本场景作用 |
|---|---|---|---|---|
| **#89812** (`79896a2`) | 2026-06-03 | mirror 失败 try/catch 成 best-effort, 不再中断发送 | ✅ 是 | 解释了为什么本地看到的是 WARN 而非抛错。但只让发送不被中断, **没解决状态泄漏 + drain 重放** |
| **#90775** (`bbfe8cc`) | 2026-06-06 | compaction 写入走 owned-write fence, 修 compaction-triggered takeover | ✅ 是 | 修了 compaction **追加**时的 fence, 但没覆盖 rotation **重命名 successor** 这条路径, 06-19/06-20 仍触发 |
| **#92123** (`1e878dd`) | ~2026-06-12 | Btrfs ctimeNs 误报, fingerprint 去掉 ctimeNs, 只留 `dev+ino+size+mtimeNs` | ❌ 否(在 alpha/beta) | 修的是文件系统 ctime 抖动误报, **不是 inode 变化**, 对本场景(compaction rotation 真·改 inode)非直接修复, 但 fingerprint 收紧更稳定 |
| **#92274** | **未合并, Open** | subagent announce 3x 重复的真正修复: 把"post-send lock-change failure"归类为永久失败, 有 send evidence 时停止重试 | ❌ 否 | **直接命中本场景**, 但被 ClawSweeper bot 拦着要求更多真实传输证据, 连 alpha 都没进 |

### 4.1 issue #91527 的关键证词

issue #91527 明确指出: "Subagent announce 3x duplicate still reproduces on 2026.6.1 release + Telegram — #89812 only fixes outbound/deliver path, not subagent-announce-delivery"。这与本调查 2026-06-20 23:01:19 看到的 `Subagent completion direct announce failed` 连发 3 次完全吻合。`subagent-announce-delivery.ts` 的重试路径是另一条独立重复来源, #89812 没覆盖, #92274 才修但未合并。

### 4.2 结论: 升级解决不了

- npm `latest` = `2026.6.8` 就是本地版本, 没有更新的 stable。
- 最近的 beta/alpha(`v2026.6.9-beta.1`、`v2026.6.19-alpha.2`)含 #92123, 但**真正针对本场景的 #92274 还没合并**, 连 alpha 都没进。
- 已合并的 #89812、#90775 已在本地, 没堵住 rotation 这条路径。

**短期升级到 beta/alpha 能拿到 #92123(fingerprint 更稳), 可能减少部分误报, 但不能根治 drain 重放和 announce 3x 重复。** 等 #92274 合并并进 stable 后再升级才有完整修复。

---

## 5. 修复方向(按 ROI, 均未打补丁)

### 5.1 mirror 失败时重试并重新解析 successor 路径(治本, 改 deliver-Cgr2aBSp.js)

`deliver-Cgr2aBSp.js:1402` 镜像调用处, 捕获 `EmbeddedAttemptSessionTakeoverError`, 重新 `resolveSessionTranscriptFile` 拿 rotation 后的 successor 路径再写一次。rotation 14-15 分钟前就发生了, 重试时新路径已稳定, 几乎必成。

注意: openclaw 是 npm 安装的 dist 产物(bundled/minified), 直接改 `dist/*.js` 会被下次 `npm update` 覆盖, 需确认有无 patch-package / postinstall hook 机制持久化, 或等上游合并。

### 5.2 delivery 状态推进与 mirror 解耦(治本, 改状态机)

Telegram `outbound send ok` 拿到 messageId 就把 entry 推到 `delivered`, 不依赖 mirror 是否成功。mirror 只是记账, 不该卡状态机。这样 drain 永远不会重放真正发出去的消息。这是 #92274 在做的方向(区分 post-send 失败 = 永久, pre-send 失败 = 可重试)。

### 5.3 drain 强制 adapter reconciliation(防御性)

把现在部分路径生效的 `refusing blind replay without adapter reconciliation` 变成 drain 的强制前置 — 重放前先查 Telegram 这条消息是否已存在。当前这个防护只在部分路径生效(06-19 16:43 的 drain 就没拦住)。

### 5.4 fingerprint 降级为可恢复(治标)

`EmbeddedAttemptSessionTakeoverError` 当前是硬抛, 对 mirror 这个"best-effort 记账"场景太重。应允许它 fallback 到无 fence 写或重试, 而不是直接放弃。#92123 已经在 fingerprint 定义层面收紧(去掉 ctimeNs), 但没改 throw 的硬性。

### 5.5 配置层缓解(不动代码)

调 `~/.openclaw/openclaw.json` 的 compaction 参数:
- 降低 `maxActiveTranscriptBytes`(默认 10MB)让 rotation 更可预测 — 但 rotation 更频繁可能反而增加撞车概率。
- 反过来调大减少 rotation 频率 — 但会推迟 compaction, 增加上下文压力(参见 context-overflow 调查)。
- 两者都不理想, 治标不治本。主要靠代码层修复。

---

## 6. 相关文件索引

### 远端 62
| 路径 | 说明 |
|---|---|
| `/tmp/openclaw/openclaw-YYYY-MM-DD.log` | gateway 运行时日志(按本地日期分文件, +07:00) |
| `~/.openclaw/agents/main/sessions/*.jsonl` | session transcript(被 compaction rotation 重命名的对象) |
| `~/.openclaw/openclaw.json` | 主配置(compaction 块) |
| `/opt/homebrew/lib/node_modules/openclaw/dist/deliver-Cgr2aBSp.js` | outbound deliver, mirror 调用点(line 1402/1405) |
| `/opt/homebrew/lib/node_modules/openclaw/dist/transcript-NdJkeRhp.js` | `appendAssistantMessageToSessionTranscript`(927)、fingerprint fence 调用、idempotency scan(746) |
| `/opt/homebrew/lib/node_modules/openclaw/dist/selection-kQiC501t.js` | `EmbeddedAttemptSessionTakeoverError`(6104)、fingerprint 校验抛错(6235)、`readSessionFileFingerprint`(6095) |
| `/opt/homebrew/lib/node_modules/openclaw/dist/subsystem-BbA2Znit.js` | 日志框架本身(WARN 行的 `path` 指向这里, 非业务逻辑) |

### 上游
| 资源 | 说明 |
|---|---|
| `github.com/openclaw/openclaw` | 上游仓库 |
| PR #89812 / issue #89626 | mirror 失败 best-effort(已合并, 已在 2026.6.8) |
| PR #90775 | compaction-triggered takeover(已合并, 已在 2026.6.8) |
| PR #92123 / issue #92109 | Btrfs ctimeNs 误报(已合并, 未进 stable) |
| PR #92274 / issue #91527 | subagent announce 3x 重复真正修复(**未合并**) |

---

## 7. 本次调查方法与数据

- **日志对照:** 对 2026-06-19/06-20 两天日志, 提取所有 `MIRROR_FAIL`、`COMPACTION`、`PRECHECK`、`DRAIN`、`DELIVERY_STATE`、`SEND_OK` 事件, 按时间窗口对照(脚本 `/tmp/oc_corr3.py`, 按 session_id + 600s/900s 窗口)。
- **源码追踪:** 从日志 WARN 的 `path` 字段出发, 确认 `subsystem-*.js` 是日志框架非业务点, 改搜消息文本 `failed to mirror outbound delivery` 定位到 `deliver-Cgr2aBSp.js`, 顺调用链追到 `transcript-NdJkeRhp.js` 和 `selection-kQiC501t.js`。
- **上游核对:** 查 npm registry 确认 latest = 2026.6.8; 查 GitHub commit search 按关键词(`EmbeddedAttemptSessionTakeoverError`、`failed to mirror outbound delivery`)定位到 4 个相关 PR, 逐一核对合并状态与 release 归属。
- **证据局限:** 用户引用的"好,先 spawn Architect 设计 #19"这条具体消息, 在 06-19/06-20 日志里没有完全一致的字符串(最接近的是 06-20 23:51 关于 #19 的"设计文档已追加...要 spawn Codex 执行吗?🔥", messageId=1955, 紧跟一次 mirror 失败)。06-21 00:45 那条若确属今天凌晨, 其日志在 06-21 文件里(调查时该文件刚生成、内容很少)。**但根因机制不依赖这一条具体消息** — 06-19/06-20 共 8 次 mirror 失败 + 06-19 16:43 一次真实 drain 重放, 已把链路坐实。
