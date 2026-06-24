# OpenClaw Duplicate Message Investigation

**Date:** 2026-06-21

**Environment:** OpenClaw gateway on rosen@172.27.15.62 (launchd: `ai.openclaw.gateway.plist`, port 18789), installed version `openclaw@2026.6.8`

**Symptom:** The Telegram bot occasionally sends the same message twice (most recent: around 2026-06-21 00:45 (+07))

**Scope:** Runtime logs (`/tmp/openclaw/openclaw-*.log`) + session transcripts (`~/.openclaw/agents/main/sessions/*.jsonl`) + openclaw dist source (`/opt/homebrew/lib/node_modules/openclaw/dist/`) + upstream repo (`github.com/openclaw/openclaw`)

---

## TL;DR

1. **Duplicates are not sporadic; they are a deterministic state-machine leak.** 2026-06-19 and 2026-06-20 each triggered 4 `failed to mirror outbound delivery into session transcript` WARNs.
2. **Root-cause chain:** compaction rotation renames the transcript file → the outbound delivery mirror's fingerprint (inode) check throws `EmbeddedAttemptSessionTakeoverError` → the delivery entry stays in `send_attempt_started` → the next reconnect-drain replays it as "not yet sent" when the adapter's unknown-send reconciliation misreports `not_sent` → the user receives a second copy.
3. **Related to but distinct from the [context-overflow investigation](./openclaw-context-overflow-investigation.md):** both touch the session transcript + compaction subsystem, but that one is "context too large, truncate tool results" (read path), while this one is "compaction rotation renamed the file and broke a concurrent mirror write" (write path).
4. **Upgrade does not fix this.** npm `latest` = `2026.6.8` is the installed version. The directly relevant fix (PR #92274) is still unmerged; already-merged #89812 and #90775 are in 2026.6.8 but do not cover the rotation path.
5. **Fix implemented and deployed.** In the fork (on `v2026.6.8` tag), `src/infra/outbound/deliver.ts`'s wrapper catch now calls `markQueuedPlatformOutcomeUnknown` (advance to `unknown_after_send`) instead of `failDelivery` when send evidence exists (`OutboundDeliveryError.sentBeforeError === true` and `platformSendStarted === true`). Without send evidence, `failDelivery` remains correct. Verified: unit tests 98/98 + negative control; built dist deployed to the 62 production gateway, healthy startup. SQLite on 62 shows 8 historical entries stuck in `recovery_state=send_attempt_started` (IDs matching the logged drain events) — direct forensic evidence of the root cause. See Section 5.

---

## 1. Symptoms and log evidence

### 1.1 Log layout

- Logs are split by local date: `/tmp/openclaw/openclaw-YYYY-MM-DD.log`, timezone `+07:00`.
- Each line is JSON with key fields `time` (ISO +07:00), `message`, `_meta.logLevelName`, `session_id`, `traceId`.
- Note: the real level lives under `_meta.logLevelName`; there is no top-level level field (openclaw-audit PR #7 fixed this parsing).

### 1.2 Two duplicate sources, both visible in logs

**Source A — reconnect drain actually replayed (user saw a duplicate)**, 2026-06-19 16:43:50:
```
16:43:50.351  [telegram][diag] Telegram reconnect drain: 1 pending message(s) matched telegram:default
16:43:50.356  [telegram][diag] Telegram reconnect drain: entry d2c9370e-... is already being recovered
16:43:50.732  telegram outbound send ok  messageId=1286  operation=sendMessage
16:43:51.318  telegram outbound send ok  messageId=1287
16:43:52.383  telegram outbound send ok  messageId=1288
16:43:53.368  telegram outbound send ok  messageId=1289
16:43:53.388  [WARN] failed to mirror outbound delivery into session transcript; channel send already succeeded: session file changed ...
```

**Source B — drain attempted replay but was guarded (no duplicate)**, 2026-06-20 23:02:04:
```
23:02:04.688  [telegram][diag] Telegram reconnect drain: 3 pending message(s) matched telegram:default
23:02:04.693  [telegram] Delivery entry 24c22059-... delivery state is send_attempt_started; refusing blind replay without adapter reconciliation
23:02:04.697  [telegram] Telegram reconnect drain: retry failed for entry 24c22059-...: delivery state is send_attempt_started
```

**Source C — sendRichMessage failure auto-retry (also produces duplicates)**, 2026-06-20 23:01:19:
```
23:01:19.847  [ERROR] telegram richMessage failed: ... RICH_MESSAGE_URL_I... (400)
23:01:19.874  Subagent completion direct announce failed for run 93e59404...
23:01:21.397  [ERROR] telegram richMessage failed: ...   ← retry 1
23:01:23.943  [ERROR] telegram richMessage failed: ...   ← retry 2
23:01:24.053  Subagent announce give up (retry-limit)
```
Classic at-least-once delivery + non-idempotent send + lost response. One of the attempts likely reached Telegram, but the response was treated as a 400, so the retry sent again.

### 1.3 messageIds are strictly monotonic, never duplicated

OpenClaw calls the Telegram API once per message; messageIds (1665→1955) are strictly increasing with no repeats. **The duplication happens on the Telegram side:** the same content is sent twice with different messageIds. This points responsibility at "OpenClaw believed the send failed and retried," not "the Telegram API accepted one send twice."

---

## 2. Root-cause mechanism (logs → source)

### 2.1 The outbound delivery pipeline has two stages

1. **Send:** call the Telegram API → on success, **mirror the delivery into the session transcript (`.jsonl`)** and advance the pending queue entry from `send_attempt_started` to `delivered` / remove it.
2. **Reconnect drain:** when the long-poll connection drops and reconnects, scan the pending queue; any entry still in `send_attempt_started` or `unknown_after_send` ("call issued, delivery unconfirmed") is a candidate for replay.

### 2.2 Drain does NOT blindly replay — it reconciles first

`drainQueuedEntry` (`dist/delivery-queue-BffjNycT.js:475`) calls `reconcileUnknownQueuedDelivery` before replaying:

```js
if (entry.recoveryState === "send_attempt_started" || entry.recoveryState === "unknown_after_send") {
    const reconciliation = await reconcileUnknownQueuedDelivery({entry, cfg, log});
    if (reconciliation?.status === "sent")      { ackDelivery(...); return "recovered"; }      // confirmed sent → drop
    if (reconciliation?.status === "not_sent")  { ...; "replaying" }                           // ← replay (duplicate source)
    else /* unresolved or null */ {
        errMsg = `delivery state is ${entry.recoveryState}; refusing blind replay without adapter reconciliation`;
        moveToFailed(...); return "failed";                                      // safe: no replay
    }
}
```

So duplicates only occur when reconciliation returns `not_sent` (a misreport). The `refusing blind replay` guard (sources B above) is the `unresolved`/`null` branch working correctly.

### 2.3 Why the entry is stuck in `send_attempt_started` — the wrapper catch

`deliverOutboundPayloadsWithQueueCleanup` (`src/infra/outbound/deliver.ts:1311`) wraps `deliverOutboundPayloadsCore`:

```ts
try {
    let platformSendStarted = false;
    const results = await deliverOutboundPayloadsCore({
        ...wrappedParams,
        ...(queueId ? { onPlatformSendStart: async () => {
            if (platformSendStarted) return;
            platformSendStarted = await markQueuedPlatformSendAttemptStarted({queueId, queuePolicy});
        } } : {}),
    });
    platformResultsReturned = true;
    ...
    // success path: markQueuedPlatformOutcomeUnknown(queueId) then ackDelivery(queueId)
} catch (err) {
    if (queueId) {
        if (isDeliveryAbortError(err)) {
            await ackDelivery(queueId).catch(() => {});
        } else if (!platformResultsReturned) {
            await failDelivery(queueId, formatErrorMessage(err)).catch(...);   // ← PROBLEM
        }
    }
    throw err;
}
```

When `deliverOutboundPayloadsCore` throws and `platformResultsReturned === false`, the wrapper calls `failDelivery`. This leaves the entry in the queue in state `send_attempt_started` (the `markQueuedPlatformOutcomeUnknown` step that would advance it to `unknown_after_send` never runs). On the next reconnect, drain sees `send_attempt_started` and reconciles; if reconciliation misreports `not_sent`, the entry is replayed → **duplicate**.

The trigger for this catch: a required-mode batch send where an earlier payload succeeded (`results.length > 0`, so `OutboundDeliveryError.sentBeforeError === true`) but a later payload throws. The earlier payload already reached Telegram, yet the entry is left as "not sent" for drain to replay.

### 2.4 Why the entry can also get stuck via the mirror path

`deliverOutboundPayloadsCore` mirrors the delivery after send (`src/infra/outbound/deliver.ts:1958`). The mirror calls `appendAssistantMessageToSessionTranscript`, which takes its own write lock and runs a fingerprint fence (`dist/selection-kQiC501t.js:6225-6236`):

```js
if (sameSessionFileFingerprint(beforeWrite, current)) { /* trust, update fence */ return; }
takeoverDetected = true;
throw new EmbeddedAttemptSessionTakeoverError(params.lockOptions.sessionFile);
```

The fingerprint includes `ino` (inode). Compaction rotation renames `uuid.jsonl` → `<timestamp>_uuid.jsonl` and creates a successor with a new inode → fingerprint mismatch → `EmbeddedAttemptSessionTakeoverError` → mirror returns `{ok:false}`.

After PR #89812 (already in 2026.6.8), mirror failure is best-effort (caught, logged as WARN, does not abort the send). So the mirror path alone no longer aborts the wrapper. But the wrapper catch (2.3) remains the hole for mid-batch failures.

### 2.5 Mirror-fail session files match compaction rotation events

| MIRROR_FAIL time | session filename timestamp (UTC) | corresponding +07 rotation | gap |
|---|---|---|---|
| 2026-06-20 00:24:42 | `2026-06-19T17-10-41Z` | 06-20 00:10:41 | 14 min |
| 2026-06-20 23:51:48 | `2026-06-20T16-36-38Z` | 06-20 23:36:38 | 15 min |

The timestamp-prefixed filenames are compaction successor archives, matching `[compaction] rotated active transcript` log events 14-15 min earlier.

### 2.6 The idempotency guard is gated behind the failing check

`appendSessionTranscriptMessageLocked` (`dist/transcript-NdJkeRhp.js:746`) has an idempotency guard:
```js
const existing = idempotencyKey && params.idempotencyLookup === "scan"
    ? await findTranscriptMessageByIdempotencyKey(params.transcriptPath, idempotencyKey) : void 0;
if (existing) return {...existing, appended: false};   // idempotent short-circuit
```
But this runs only after the write lock is acquired. The fingerprint fence throws during lock acquisition, so the idempotency guard never runs. **The guard that would prevent duplicates is gated behind the check that fails.**

---

## 3. Relationship to the context-overflow investigation

The [context-overflow investigation](./openclaw-context-overflow-investigation.md) covers **precheck overflow → truncate tool results** (prompt too large; truncate before sending). Both investigations share the session transcript + compaction subsystem, but the failure modes are orthogonal:

| | context-overflow investigation | this investigation (duplicate messages) |
|---|---|---|
| Symptom | task feels "interrupted" mid-execution | one message arrives twice on Telegram |
| Root cause | prompt exceeds 202k tokens → precheck truncates tool results | mid-batch send failure leaves entry in `send_attempt_started`; drain reconciles `not_sent` and replays |
| Path | context management (precheck/compact) | outbound/deliver + delivery queue |
| Direction | **read** transcript to decide truncation | **write** transcript / queue state |
| Source files | `attempt.tool-run-context-*.js`, `tool-result-truncation-*.js` | `deliver.ts`, `delivery-queue-*.js`, `selection-*.js` |

**precheck is not the trigger for this bug:** a 600s window around each MIRROR_FAIL showed zero `[context-overflow-precheck]` events. The two failure modes do not coincide.

---

## 4. Upstream fix status (checked 2026-06-21)

Installed version `openclaw@2026.6.8` = npm dist-tag `latest`. Related upstream PRs:

| PR | merged | fix | in 2026.6.8? | relevance |
|---|---|---|---|---|
| **#89812** (`79896a2`) | 2026-06-03 | mirror failure try/catch best-effort, no longer aborts send | ✅ yes | explains why we see WARN not a throw; does not address the delivery-state leak / drain replay |
| **#90775** (`bbfe8cc`) | 2026-06-06 | compaction append routed through owned-write fence | ✅ yes | fixes compaction-append fence; does not cover the wrapper catch for mid-batch failures |
| **#92123** (`1e878dd`) | ~2026-06-12 | Btrfs ctimeNs false positive; drop ctimeNs from fingerprint | ❌ alpha/beta | filesystem ctime drift, not inode change; not a direct fix for this scenario |
| **#92274** | **unmerged, Open** | subagent announce 3x duplicate: classify post-send lock-change as permanent when send evidence exists | ❌ no | directly relevant to announce path; not merged (blocked on real-transport proof) |

### 4.1 The gap this PR fills

#92274 fixes the **subagent-announce-delivery** retry path (`src/agents/subagent-announce-delivery.ts`). #89812 fixes the **mirror** best-effort path. Neither addresses the **outbound/deliver wrapper catch** (`src/infra/outbound/deliver.ts:1388`): when a required-mode batch send fails mid-batch with `sentBeforeError === true`, the entry is `failDelivery`-ed into `send_attempt_started`, which drain later replays. This PR closes that third path.

---

## 5. The fix

### 5.1 Change

In `deliverOutboundPayloadsWithQueueCleanup`'s catch block, when `!platformResultsReturned` and the error carries send evidence (`OutboundDeliveryError` with `sentBeforeError === true` and `platformSendStarted === true`), call `markQueuedPlatformOutcomeUnknown` instead of `failDelivery`. This advances the entry to `unknown_after_send` so that reconnect drain routes it through `reconcileUnknownQueuedDelivery` (query the adapter for actual send state) rather than leaving it in `send_attempt_started` for blind replay.

When there is no send evidence (`sentBeforeError === false`), `failDelivery` remains correct — nothing reached the channel, so leaving the entry for retry is safe.

### 5.2 Why `unknown_after_send` and not `ackDelivery`

`ackDelivery` would silently drop the entry, risking a lost message if the partial send actually failed to reach the channel. `unknown_after_send` preserves the entry for drain to reconcile against the adapter: confirmed `sent` → ack; `not_sent` → replay (now legitimately); `unresolved` → `refusing blind replay`. This is the safe middle ground — it trusts the adapter's reconciliation rather than guessing.

### 5.3 Tests

- `marks queued delivery as unknown-after-send (not failed) when a later payload fails after an earlier one succeeded` — the regression test: two payloads, first succeeds, second throws; asserts `markQueuedPlatformOutcomeUnknown` called, `failDelivery` and `ackDelivery` not called.
- `still calls failDelivery when a payload fails before any send succeeded` — guard test: no send evidence → `failDelivery` remains correct.

### 5.4 Verification results (2026-06-21)

| Check | Result |
|---|---|
| Unit tests (mocked queue) | pass 98/98, including 2 new tests (regression + guard); negative control: reverting the patch makes the regression test fail |
| Integration tests (real SQLite queue) | pass 2/2 in `deliver.queue-integration.test.ts` — uses the real `delivery-queue` (no mock) and real `deliverOutboundPayloads` code path. Mid-batch failure with send evidence -> `recovery_state=unknown_after_send`, `retryCount=0`; no send evidence -> `failDelivery`, `recovery_state=send_attempt_started`. Negative control: reverting `deliver.ts` to v2026.6.8 original makes the mid-batch test fail with `recovery_state=send_attempt_started` (the root-cause state), proving the test exercises the patch at the real-queue layer. |
| Negative control | with the patch reverted, the regression test fails (`markDeliveryPlatformOutcomeUnknown` call count 0) |
| Full build | `pnpm build` 203.7s; built dist contains the patch fingerprint (`platform-outcome-unknown after mid-send error`) |
| Version match | fork build = `2026.6.8` = version installed on 62 |
| Deploy to 62 | backed up original dist (`dist.bak-20260621-103602`), rsync overlay, gateway restarted cleanly (PID 8378) |
| Startup health | heartbeat / cron / telegram provider / polling ingress all started, no errors |
| Forensic evidence | SQLite `delivery_queue_entries` on 62 has 8 historical rows with `status=failed, recovery_state=send_attempt_started`; IDs match the logged drain events one-to-one (24c22059 / ae488190 / 838243b5 = 06-20 23:02; d3a41f6e / 5c8d150d / 560d8dff = 06-19 23:55; b78a38b2 / 6c3bdff4 = 06-16) |
| End-to-end (direct-send path) | observed 2026-06-21 12:39:27: `outbound send ok messageId=1992` then `[WARN] failed to mirror ... session file changed` (session `93f749ae`, the telegram对话 compaction-successor). NO new `delivery_queue_entries` row, NO reconnect drain, NO duplicate; `task_runs.delivery_status=delivered`. This was a **non-queued direct send** (`operation=sendRichMessage`, no queueId) — mirror failure on this path does not risk drain replay because drain only scans the queue. The patch (queued path) was not exercised here. |
| End-to-end (queued path) | pending: waiting for a mirror-fail on a QUEUED send (one that creates a `delivery_queue_entries` row); then verify `recovery_state` is `unknown_after_send`, not `send_attempt_started`. The 8 historical `send_attempt_started` rows are all pre-patch (06-16/19/20). |

### 5.5 Out of scope (not fixed by this PR)

- The mirror-path `EmbeddedAttemptSessionTakeoverError` itself (compaction rotation changes inode). #89812 already makes mirror best-effort; a deeper fix would retry mirror with a re-resolved successor path, but that is independent of the drain-replay hole this PR closes.
- The `reconcileUnknownQueuedDelivery` `not_sent` misreport itself (adapter reconciliation reliability). This PR avoids depending on it for the `send_attempt_started` case by advancing to `unknown_after_send` proactively, but does not change the adapter's reconciliation logic.
- **Mirror failure on the non-queued direct-send path.** Not all outbound sends go through the delivery queue: direct sends (`operation=sendRichMessage` with no queueId, no `delivery_queue_entries` row) still run the transcript mirror and still log `failed to mirror ... session file changed` on fingerprint mismatch (observed 2026-06-21 12:39:27, messageId=1992). But because drain only scans `delivery_queue_entries`, a mirror failure on a non-queued send has no entry to replay -- it cannot produce a duplicate. Only the queued path risks drain replay, so only the queued path needs this fix. The direct-send path's mirror failure is purely lost transcript bookkeeping (a separate, lower-severity issue).

---

## 6. File index

### Remote host (rosen@172.27.15.62)
| path | what |
|---|---|
| `/tmp/openclaw/openclaw-YYYY-MM-DD.log` | gateway runtime logs (split by local date, +07:00) |
| `~/.openclaw/agents/main/sessions/*.jsonl` | session transcripts (renamed by compaction rotation) |
| `~/.openclaw/openclaw.json` | main config (compaction block) |
| `/opt/homebrew/lib/node_modules/openclaw/dist/deliver-Cgr2aBSp.js` | outbound deliver, mirror call site |
| `/opt/homebrew/lib/node_modules/openclaw/dist/delivery-queue-BffjNycT.js` | delivery queue, `drainQueuedEntry` + `reconcileUnknownQueuedDelivery` |
| `/opt/homebrew/lib/node_modules/openclaw/dist/transcript-NdJkeRhp.js` | `appendAssistantMessageToSessionTranscript`, idempotency scan |
| `/opt/homebrew/lib/node_modules/openclaw/dist/selection-kQiC501t.js` | `EmbeddedAttemptSessionTakeoverError`, fingerprint fence |

### Upstream
| resource | what |
|---|---|
| `github.com/openclaw/openclaw` | upstream repo |
| PR #89812 / issue #89626 | mirror failure best-effort (merged, in 2026.6.8) |
| PR #90775 | compaction-triggered takeover (merged, in 2026.6.8) |
| PR #92123 / issue #92109 | Btrfs ctimeNs false positive (merged, not in stable) |
| PR #92274 / issue #91527 | subagent announce 3x duplicate (unmerged) |

---

## 7. Method and evidence limitations

- **Log correlation:** extracted all `MIRROR_FAIL`, `COMPACTION`, `PRECHECK`, `DRAIN`, `DELIVERY_STATE`, `SEND_OK` events for 2026-06-19/06-20 and correlated by `session_id` within 600s/900s windows.
- **Source tracing:** started from the WARN's `path` field, confirmed `subsystem-*.js` is the logging framework (not business logic), searched the message text `failed to mirror outbound delivery` to locate `deliver-Cgr2aBSp.js`, then followed the call chain to `transcript-NdJkeRhp.js` and `selection-kQiC501t.js`. The drain/reconcile logic was located by searching `send_attempt_started` → `delivery-queue-BffjNycT.js`.
- **Upstream check:** confirmed npm `latest` = 2026.6.8; GitHub commit search by keyword (`EmbeddedAttemptSessionTakeoverError`, `failed to mirror outbound delivery`) located 4 related PRs; verified merge status and release inclusion for each.
- **Evidence limitation:** the specific message quoted by the user ("好,先 spawn Architect 设计 #19") has no exact string match in the 06-19/06-20 logs (closest: 06-20 23:51, #19 design summary, messageId=1955, immediately followed by a mirror failure). The 06-21 00:45 message's log would be in the 06-21 file (minimal content at investigation time). **The root-cause mechanism does not depend on this single message** — 8 mirror failures across 06-19/06-20 plus one real drain replay (06-19 16:43) establish the chain.
