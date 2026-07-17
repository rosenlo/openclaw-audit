# Topology — openclaw-audit

## 1. Repo layout and related repos

**Audit repo (this one):** `/Users/user/work/openclaw-audit`
- Remote: `origin` → `github.com/rosenlo/openclaw-audit.git`
- Purpose: small Python tool for auditing local OpenClaw + LiteLLM logs
- Layout:
  - `openclaw-audit.py` — thin CLI / web / watch wrapper
  - `openclaw_audit/` — package with all logic (analyze, classify, config, insights, parsing, queries, render, util); public exports locked by `tests/test_openclaw_audit.py`
  - `tests/` — pytest suite
  - `docs/` — investigation notes (currently `openclaw-duplicate-message-investigation.md` and `openclaw-context-overflow-investigation.md`)
  - `.env.example` — env vars: `OPENCLAW_HOME`, `OPENCLAW_LOG_DIR`, `OPENCLAW_GATEWAY_LOG`, `LITELLM_DIR`, `OPENCLAW_AUDIT_TZ`, `OPENCLAW_NODE`, `OPENCLAW_CLI`

**Upstream openclaw repo (frequently referenced):** `github.com/openclaw/openclaw`
- Our fork: `github.com/rosenlo/openclaw` at `/Users/user/github/rosenlo/openclaw` (shallow clone, ~200 commits)
- Fork is for contributing upstream PRs; do not push archival tags into it (lesson from 2026-07-17)
- Fork has its own `memory/` at `/Users/user/github/rosenlo/openclaw/memory/` (separate from this audit repo's memory)
- Upstream remotes:
  - `origin` → `github.com/rosenlo/openclaw.git` (the fork)
  - `upstream` → `github.com/openclaw/openclaw.git`

**Local OpenClaw install (audit target):**
- Binary install via npm at `/opt/homebrew/lib/node_modules/openclaw/dist/`
- Logs at `/tmp/openclaw/openclaw-*.log` (rotated daily, JSON lines)
- Sessions at `~/.openclaw/agents/main/sessions/*.jsonl` (legacy; SQLite-backed after 2026.7.2-beta.1)
- State at `~/.openclaw/state/openclaw.sqlite`
- Optional gateway log at `~/Library/Logs/openclaw/gateway.log`

## 2. Identity

- GitHub identity for this repo and the fork: `rosenlo / rosenluov@gmail.com`
- `GITHUB_TOKEN` env var (if set) overrides gh keychain token — `unset GITHUB_TOKEN` before `gh` calls
- No `Co-Authored-By` AI trailers in commits (global rule)

## 3. Upstream issue tracker reference

- Canonical umbrella for duplicate Telegram messages: `openclaw/openclaw#96242`
- We have posted retractions/corrections there as recently as 2026-07-17
- ClawSweeper bot maintains a sticky review comment with status tables — re-runs edit in place

**Source:** 2026-07-17 session.
