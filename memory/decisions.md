# Decisions — openclaw-audit

## 1. Withdrew Path 3 framing from upstream #96242 rather than re-investigating live

**Context:** 2026-07-17 re-investigation found that the "Path 3 = sendRichMessage at-least-once retry" framing was wrong (strict retry guard has been in place since 2026-03-08, well before our 2026-06-22 repro). The 2026-06-22 transcript evidence (session `72f581df`, real duplicate) still has no identified root cause. Options: (a) post a retraction now to stop the wrong framing from spreading, (b) silently re-investigate the real root cause before posting, (c) leave the framing up until we know the real cause.

**Decision:** post a retraction immediately. Distinguish clearly between "the duplicate is real" and "the explanation was wrong." Don't claim a new root cause until we have one. Mark Path 3 as "root-cause hypothesis invalidated, real cause unidentified, re-investigation in progress."

**Alternatives considered:**
- Silent re-investigation then post — risks Eva2026DE and any other third parties building on the wrong framing for longer
- Leave framing up until cause known — would be dishonest and would mislead maintainers' triage

**Consequences:**
- Comment `5004551955` posted 2026-07-17 on #96242 publicly withdraws the framing
- Issue cannot be closed yet (the underlying duplicate is real), so #96242 should stay open until either the real cause of 2026-06-22 transcript is found, or Eva2026DE confirms they were on a pre-2026.6.10 build (which would attribute their repro to Path 2 / #96247, not Path 3)
- This audit repo's `docs/openclaw-duplicate-message-investigation.md` Section 1.2 "Source C" should be updated to reflect that the retry hypothesis was invalidated, but is preserved as a historical hypothesis

**Source:** 2026-07-17 session; #96242 comment 5004551955.

## 2. Path 4 patch archived rather than pushed upstream

**Context:** Path 4 patch (`2fe805a8ad` on the deleted fork branch) was complete and tested, but never pushed. Upstream #98236 (SQLite flip, merged 2026-07-11, shipped 2026.7.2-beta.1) eliminated Path 4's primary trigger. The residual race window shrank from seconds-scale to milliseconds-scale. Options: (a) rebase + push upstream as defense-in-depth, (b) archive and revisit only if production shows the residual race, (c) keep the branch forever as a placeholder.

**Decision:** archive. Delete the worktree and branch; preserve the commit hash in memory/gotchas.md; revisit only if the residual race is observed on 2026.7.2-beta.1+ in production.

**Alternatives considered:**
- Push as defense-in-depth — not justified by the current evidence; would burn maintainer review bandwidth on a non-live bug
- Keep branch forever — adds clutter to the fork; the commit is preserved in reflog for ~30 days and the approach is documented in memory

**Consequences:**
- Worktree `.claude/worktrees/path4-send-confirmation` and branch `fix/pending-final-delivery-send-confirmation` deleted from rosenlo/openclaw fork on 2026-07-17
- Approach documented in `memory/gotchas.md` entry 3 with API drift notes; if revisited, the patch needs rework against post-#98236 surface (SQLite accessor `updateSessionEntry`, moved files)
- No upstream PR for Path 4 will be opened from this audit repo unless production evidence demands it

**Source:** 2026-07-17 session, Task B evaluation; PR #98236.
