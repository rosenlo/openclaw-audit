# AGENTS.md

## Repo Intent

This repo is a small, mostly self-contained audit tool for local OpenClaw setups. Keep changes pragmatic and easy to run on a personal machine.

## Working Rules

- Preserve the current local-first behavior. Do not add networked telemetry.
- Keep machine-specific paths behind environment variables.
- Prefer standard-library Python unless an added dependency is clearly worth it.
- Treat Flask as optional for the dashboard path.
- Avoid breaking the single-file CLI unless there is a strong reason to split modules.

## When Editing

- Update `README.md` when flags, env vars, or setup steps change.
- Keep examples runnable with plain `python3`.
- Be careful not to reintroduce hardcoded personal identifiers or machine paths.
- If you add new outputs, keep privacy in mind because logs may contain personal conversation metadata.
