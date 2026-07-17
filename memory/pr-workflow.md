# PR Workflow — openclaw-audit

## 1. This audit repo

- Remote: `origin` → `github.com/rosenlo/openclaw-audit.git`
- Single canonical branch: `main`
- Recent commits (PR #8–#17) show a squash-merge style with conventional-commit prefixes (`feat:`, `fix:`, `docs:`)
- PRs numbered from #1; small repo, no fork model — push directly to `origin/main` for trivial work, use a feature branch for anything substantial
- Tests are required: `python3 -m pytest tests/ -q` must pass before commit
- Update `README.md` when flags, env vars, or setup steps change (per root `AGENTS.md`)
- No `Co-Authored-By` AI trailers in commits (global rule)

## 2. Cross-repo: contributing to upstream openclaw

When preparing a PR for `openclaw/openclaw`, work in the rosenlo fork at `/Users/user/github/rosenlo/openclaw`:
- Use git worktrees under `.claude/worktrees/` for PR-bound branches (see fork's `memory/gotchas.md` for prior PR lessons)
- Rebase on canonical upstream `main` before pushing
- Clean up worktree + branch after merge or after abandoning the PR
- Identity: `rosenlo / rosenluov@gmail.com`
- `GITHUB_TOKEN` env var (if set) overrides gh keychain — `unset GITHUB_TOKEN` before `gh` calls
- Inline comments must be 1-3 lines, no history/lore (fork's `memory/gotchas.md` entry 1, lesson from #96247 maintainer follow-up)
- For GHE hosts (git.toolsfdg.net), use `GH_HOST=git.toolsfdg.net gh ...` to avoid the host-routing miss

## 3. Upstream issue #96242 etiquette

We have posted on `openclaw/openclaw#96242` as `rosenlo` multiple times (2026-06-25, 2026-07-17 ×2, and the 2026-07-17 retraction). ClawSweeper bot maintains a sticky review comment that re-runs edit in place. When posting:
- Distinguish "the duplicate is real" from "the explanation is X" — they are independent claims
- Cite specific commits and file:line references so maintainers can verify
- Avoid asserting a fix is "landed" without checking the actual installed version's git history (Path 3 misdiagnosis came from asserting sendRichMessage retry was loose when it had been strict since 2026-03-08)

**Source:** 2026-07-17 session.
