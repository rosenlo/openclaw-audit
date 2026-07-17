# Ops — openclaw-audit

## 1. Common verification commands

**Run audit CLI (last hour):**
```bash
python3 openclaw-audit.py
```

**Wider time range:**
```bash
python3 openclaw-audit.py --since 24h
python3 openclaw-audit.py --since today
python3 openclaw-audit.py --since 2026-06-18
```

**Run tests:**
```bash
cd /Users/user/work/openclaw-audit && python3 -m pytest tests/ -q
```
The test suite includes a public-export lock on `openclaw_audit/__init__.py` — adding/removing exports there will fail tests unless intentional.

**Run web dashboard:**
```bash
python3 openclaw-audit.py --web
```
Requires Flask.

## 2. Upstream investigation queries (against rosenlo/openclaw fork)

**Unset GITHUB_TOKEN first** (it overrides gh keychain):
```bash
unset GITHUB_TOKEN
```

**List commits that touched a file (handles shallow clone correctly):**
```bash
gh api 'repos/openclaw/openclaw/commits?path=<file>&per_page=N' \
  --jq '.[] | {sha: .sha[0:10], date: .commit.author.date, msg: (.commit.message | split("\n")[0])}'
```

**Fetch file content at an arbitrary commit (quote the URL — zsh glob-expands `?`):**
```bash
gh api "repos/openclaw/openclaw/contents/<file>?ref=<sha>" --jq '.content' | base64 -d
```

**Full PR diff:**
```bash
gh api -H 'Accept: application/vnd.github.v3.diff' repos/openclaw/openclaw/pulls/<n>
```

**PR file list + body:**
```bash
gh pr view <n> --repo openclaw/openclaw --json files,body
```

**Post issue comment as rosenlo:**
```bash
gh issue comment <issue> --repo openclaw/openclaw --body-file <path-to-md>
```

## 3. State checks on a production OpenClaw install

**Inspect queued delivery entries stranded in `send_attempt_started`** (Path 2 forensic evidence):
```sql
SELECT id, status, recovery_state, created_at, updated_at
FROM delivery_queue_entries
WHERE status='failed' AND recovery_state='send_attempt_started'
ORDER BY created_at DESC LIMIT 50;
```
Run against `~/.openclaw/state/openclaw.sqlite`. Pre-2026.6.10 builds could accumulate these; post-#96247 (Path 2 fix) they should be moved to `unknown_after_send` or `failed` instead.

**Source:** 2026-07-17 session.
