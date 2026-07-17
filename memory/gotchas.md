# Gotchas — openclaw-audit

## 1. Path 3 "sendRichMessage at-least-once retry" was a misdiagnosis — strict retry has guarded sends since 2026-03-08

**Symptom:** 2026-06-22 transcript evidence (session `72f581df`, two identical messages received at 23:38 +08) was attributed to `sendRichMessage` retrying transient 5xx/timeout after the message already reached Telegram. The 2026-06-24 handoff and 2026-06-25 / 2026-07-17 #96242 comments all pushed this "Path 3" framing. Eva2026DE followed on 2026-06-30 saying "Path 3 reproduces reliably," but was echoing our framing.

**Root cause:** the framing was based on reading `createTelegramRetryRunner`'s existence without checking the actual `shouldRetry` predicate or the `strictShouldRetry` flag. In reality:
- `strictShouldRetry: true` was added to `src/infra/retry-policy.ts` by `3987ca4099` (2026-03-08, "refactor(retry): simplify telegram shouldRetry composition")
- `sendMessage`/`sendPoll`/`createForumTopic` were switched to `shouldRetry: isSafeToRetrySendError` + `strictShouldRetry: true` by `eb09d8dd71` the same day (landed PR #34238 by @hal-crackbot)
- `sendRichMessage` (born 2026-06-13 in `547cc0f109`) used the strict `createTelegramDeliverySendRetry` runner from day one
- `isSafeToRetrySendError` (`extensions/telegram/src/network-errors.ts:208`) only matches pre-connect failures (ECONNREFUSED / ENOTFOUND / EAI_AGAIN / ENETDOWN / ENETUNREACH / EHOSTUNREACH / UND_ERR_CONNECT_TIMEOUT) plus 421 Misdirected. It explicitly does NOT match ECONNRESET, ETIMEDOUT, 5xx — those would cause duplicates if retried.

So the 2026-06-22 duplicate cannot have been produced by the retry mechanism we blamed. The real root cause is still unidentified (the duplicate is real, the explanation was wrong). The "Path 3" framing was publicly retracted in upstream #96242 on 2026-07-17 (comment id `5004551955`).

**Fix:** before claiming a retry-based root cause, verify (a) `strictShouldRetry` flag is set in the call site, (b) the named `shouldRetry` predicate matches the error class you're blaming, (c) the introducer commit predates your repro date. The presence of a retry runner is not sufficient — `strictShouldRetry: true` narrows the retry set to pre-connect failures only. Check `extensions/telegram/src/bot/delivery.send.ts` and `extensions/telegram/src/network-errors.ts:208` for the live predicates. This audit tool's `docs/openclaw-duplicate-message-investigation.md` Section 1.2 "Source C — sendRichMessage failure auto-retry" should be treated as a documented hypothesis, not a confirmed root cause.

**How to verify:** `gh api "repos/openclaw/openclaw/contents/extensions/telegram/src/bot/delivery.send.ts?ref=<ref>" --jq '.content' | base64 -d | rg 'strictShouldRetry|isSafeToRetry'` — should show strict runner on any post-2026-03-08 ref. For a "duplicate blamed on retry" hypothesis, the named `shouldRetry` predicate must match the error class you're blaming.

**Status:** fixed (framing withdrawn upstream on 2026-07-17; real root cause of the 2026-06-22 transcript still unidentified)

**Source:** 2026-07-17 session, Path 3 re-investigation; #96242 comment 5004551955; PR #34238 / commit `eb09d8dd71`.

## 2. Shallow clone `git log -S` misattributes to the visible tip — use GitHub API for blame

**Symptom:** On the rosenlo/openclaw fork (shallow, 200 commits), `git log -S "strictShouldRetry" -- extensions/telegram/src/bot/delivery.send.ts` returned `7be5d78fd0 feat(workspaces): add full-bleed single-widget tabs (#109627)` as the introducer — clearly wrong, since `7be5d78fd0` is just the latest tip reachable in the shallow set.

**Root cause:** shallow clones truncate history; `git log -S <string>` and `git blame` cannot walk past the shallow boundary, so any string that exists today appears to be introduced by whatever commit in the shallow set last touched the file. The "exhaustive rename detection was skipped due to too many files" warning is a giveaway.

**Fix:** for accurate blame on a shallow clone, skip local git and use the GitHub API:
- `gh api 'repos/openclaw/openclaw/commits?path=<file>&per_page=N'` — list commits that touched a file
- `gh api "repos/openclaw/openclaw/contents/<file>?ref=<sha>" --jq '.content' | base64 -d` — fetch file content at an arbitrary commit (quote the URL; zsh glob-expands `?`)
- `gh api -H 'Accept: application/vnd.github.v3.diff' repos/openclaw/openclaw/pulls/<n>` — full PR diff
- `gh pr view <n> --repo openclaw/openclaw --json files,body` — PR file list + description
Or unshallow once with `git fetch --unshallow upstream` if you'll be doing many blame queries.

**How to verify:** `git rev-parse --is-shallow-repository` returns `true` → don't trust local `git log -S` / `git blame`; go to the GitHub API.

**Status:** fixed (workflow updated)

**Source:** 2026-07-17 session, Path 3 investigation; confirmed introducer was #34238 (commit `eb09d8dd71`, 2026-03-08) found via `gh api` commits-by-path, not local `git log -S`.

## 3. Path 4 patch archived — residual SIGTERM race too narrow to justify upstream PR after SQLite flip

**Symptom:** Path 4 patch (commit `2fe805a8ad` on the now-deleted branch `fix/pending-final-delivery-send-confirmation` in the rosenlo/openclaw fork) added a `pendingFinalDeliverySendConfirmedAt` field, fast-written immediately after send success and before the slow clear, so restart-recovery could skip replay if SIGTERM landed between the two writes. Patch was complete (9 files, 143/6 line diff, tsgo/lint/format/tests green) but never pushed upstream.

**Root cause (why archived):** #98236 (SQLite sessions/transcripts flip, merged 2026-07-11, shipped 2026.7.2-beta.1) eliminated Path 4's **primary** trigger — compaction rotation no longer changes inodes, so the `EmbeddedAttemptSessionTakeoverError` mirror failure that used to strand `pendingFinalDelivery=true` cannot occur on SQLite-backed sessions. The **residual** race (SIGTERM between send success and `clearPendingFinalDeliveryAfterSuccess` write completion) is structurally still present (`main-session-restart-recovery.ts:1713-1734` still replays `pendingFinalDeliveryText` unconditionally), but its window shrank from seconds-scale (mirror failure to next clear cycle) to milliseconds-scale (clear write in flight), making the patch defense-in-depth rather than a fix for a live bug.

**Fix:** patch is archived, not pursued. Branch and worktree deleted on 2026-07-17. Commit `2fe805a8ad` is preserved in git reflog (~30 days). If the residual race becomes a live concern again, the approach is still valid but the patch needs rework against the post-#98236 API surface:

- API drift after #98236 (4 of 9 files moved/renamed):
  - `clearPendingFinalDeliveryAfterSuccess` moved from `dispatch-from-config.ts` to `dispatch-from-config.pending-final.ts:81`, now uses `updateSessionEntry` (SQLite accessor) not `updateSessionStoreEntry`
  - post-run cleanup moved from `src/agents/agent-command.ts:2351` to `src/agents/command/post-run.ts:334`
  - `applyRestartRecoveryLifecycle` (used in our `main-session-restart-recovery.ts` mod) is replaced by direct `updateSessionEntry` calls
- `pendingFinalDeliverySendConfirmedAt` field does NOT exist on new main — would need to re-add to `src/config/sessions/types.ts`, `store-load.ts`, `session-entry-slot-keys.ts`

**How to verify:** if revisiting: `rg "pendingFinalDelivery === true && entry.pendingFinalDeliveryText" src/agents/main-session-restart-recovery.ts` — if that replay block is still there, residual race is still there. Then check `clearPendingFinalDeliveryAfterSuccess` call site for write atomicity.

**Status:** archived (patch preserved in fork reflog, not pursued upstream)

**Source:** 2026-07-17 session, Task B evaluation; PR #98236 (commit `0a8e3604`, 2026-07-11).
