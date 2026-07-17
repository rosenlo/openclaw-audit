# AGENTS.md

## Repo Intent

This repo is a small, mostly self-contained audit tool for local OpenClaw setups. Keep changes pragmatic and easy to run on a personal machine.

## Working Rules

- Preserve the current local-first behavior. Do not add networked telemetry.
- Keep machine-specific paths behind environment variables.
- Prefer standard-library Python unless an added dependency is clearly worth it.
- Treat Flask as optional for the dashboard path.
- Logic lives in the `openclaw_audit/` package; `openclaw-audit.py` is a thin CLI/web/watch wrapper. Add new logic to the package, keep the module dependency DAG cycle-free, and update `openclaw_audit/__init__.py` (its public exports are locked by a test) when the surface changes.

## When Editing

- Update `README.md` when flags, env vars, or setup steps change.
- Keep examples runnable with plain `python3`.
- Be careful not to reintroduce hardcoded personal identifiers or machine paths.
- If you add new outputs, keep privacy in mind because logs may contain personal conversation metadata.

## Cross-session Memory

This repo uses a cross-session memory system under `memory/`. Both
OpenCode and Claude Code read/write the same files.

- @memory/MEMORY.md — index of all notes (read at session start)
- @memory/gotchas.md — runtime traps and investigation pitfalls (READ before touching parsers or investigation docs)
- @memory/decisions.md — audit-scope and upstream-PR decisions
- @memory/topology.md — repo layout, remotes, related repos (openclaw fork)
- @memory/ops.md — commands and verification queries
- @memory/pr-workflow.md — branch/PR workflow for this audit repo and the upstream fork

When you discover something durable, append to the right `memory/*.md`
file using the entry template. Don't ask the user — just write if it's
clearly durable. The OpenCode `memory.js` plugin reminds you at
`session.idle` and `session.compacting`.
