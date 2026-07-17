# Memory Index — openclaw-audit

Cross-session memory for this repo. Shared by OpenCode and Claude Code.
Version-controlled with the repo. Both tools read/write here.

## Files

- [gotchas.md](gotchas.md) — runtime traps and investigation pitfalls
- [decisions.md](decisions.md) — tool design and audit-scope decisions
- [topology.md](topology.md) — repo layout, remotes, related repos
- [ops.md](ops.md) — commands and verification queries
- [pr-workflow.md](pr-workflow.md) — branch/PR workflow for this audit repo

## Workflow

### At session start
1. Read this file (index).
2. Read `gotchas.md` if touching parsers, classification, or investigation docs.
3. Read `topology.md` if touching PR workflow or related repos (openclaw fork).
4. Read any file the user references or that matches your task.

### During session
When you discover something durable (gotcha, decision, pattern):
1. Pick the right file (gotchas / decisions / topology / ops / pr-workflow).
2. Append a new section using the entry template (see global AGENTS.md).
3. Update this index if you created a new file.

### At session end (or when work is merged)
- Convert session-specific handoff notes into durable memory entries.
- Remove obsolete entries that no longer reflect current state.
- If you created scratch notes outside `memory/`, move durable insights in.

## Capacity

Each `memory/*.md` file ≤150 lines. When a file approaches the ceiling,
consolidate redundant entries instead of appending. See global AGENTS.md
for the full capacity-management protocol.
