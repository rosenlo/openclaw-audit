# openclaw-audit

Audit the Telegram -> OpenClaw -> LiteLLM request path from local logs.

`openclaw-audit` is a small Python tool for checking message flow, LLM failures, timeouts, context overflows, session usage, and LiteLLM gateway health from an OpenClaw setup. It can run as:

- a CLI report
- a lightweight local web dashboard
- a watch mode for continuous monitoring

## Features

- Parse `openclaw-*.log` and LiteLLM logs
- Summarize Telegram traffic, LLM errors, failovers, and context overflow events
- Inspect active OpenClaw session context usage via CLI
- Read selected OpenClaw SQLite stats when available
- Serve a local dashboard with recent events and basic health signals

## Requirements

- Python 3.11+
- Flask only if you want `--web`
- A local OpenClaw install and readable logs

## Quick Start

Run the CLI report for the last hour:

```bash
python3 openclaw-audit.py
```

Show a wider time range:

```bash
python3 openclaw-audit.py --since 24h
python3 openclaw-audit.py --since today
python3 openclaw-audit.py --since yesterday
python3 openclaw-audit.py --since 2026-06-18
```

Start the local dashboard:

```bash
python3 -m pip install flask
python3 openclaw-audit.py --web
```

Run in watch mode:

```bash
python3 openclaw-audit.py --watch --hours 3 --interval 30
```

Force-rotate litellm logs (copytruncate, regardless of size):

```bash
python3 openclaw-audit.py --rotate-litellm-logs
```

## Environment Variables

All paths are overrideable so the repo stays portable and does not depend on one machine layout.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENCLAW_HOME` | `~/.openclaw` | OpenClaw home directory |
| `OPENCLAW_LOG_DIR` | `/tmp/openclaw` | Directory containing `openclaw-*.log` |
| `OPENCLAW_GATEWAY_LOG` | auto-detect | Gateway log path |
| `LITELLM_DIR` | `~/litellm` | LiteLLM log directory |
| `LITELLM_LOG_MAX_SIZE_BYTES` | `52428800` (50 MB) | Rotate litellm logs above this size at startup |
| `LITELLM_LOG_KEEP` | `5` | Number of rotated litellm log backups to keep |
| `OPENCLAW_AUDIT_TZ` | auto-detect from system | Audit timezone override: `+HH:MM`, `-HH:MM`, `+HHMM`, `+HH`, or `UTC` (supports half-hour offsets like `+05:30`). When unset, the audit follows the system local timezone (via `datetime.now().astimezone()`), so the dashboard "最后更新" / report "生成时间" suffix matches the host's actual tz instead of a hardcoded value. |
| `OPENCLAW_NODE` | `node` | Node.js executable |
| `OPENCLAW_CLI` | `openclaw` | OpenClaw CLI executable |

Copy the example file if you want a local env file:

```bash
cp .env.example .env
```

## LiteLLM log format

LiteLLM's `err.log` is written by its in-process `logging` formatter. The default formatter uses `datefmt="%H:%M:%S"` (time-of-day only, no date), which is fragile — a cumulative `err.log` ends up mixing old text lines with current JSON lines, and dateless text lines were previously mis-stamped as "today", drifting historical events into the future.

This tool only parses JSON lines (one JSON object per line with a full ISO-8601 `timestamp` including the date). Legacy `HH:MM:SS - LiteLLM ...:` text-format lines are skipped. To make `err.log` JSON-only:

- Set `JSON_LOGS=true` in the LiteLLM process environment (it emits a JSON object per line with a full `timestamp` field). `LITELLM_LOG=DEBUG` controls the log level separately.
- Note: it's `JSON_LOGS`, **not** `LITELLM_LOG=JSON` — the latter is parsed as a log level and will raise.

## LiteLLM log rotation

When LiteLLM is launched via `launchd` (macOS) or `systemd` (Linux) with stdout/stderr redirected to a file, the supervisor does not rotate those files — `litellm.err.log` and `litellm.out.log` grow unbounded and accumulate stale text-format history from before the `JSON_LOGS=true` flip.

This tool rotates both files automatically at startup using copytruncate:

- If a log file exceeds `LITELLM_LOG_MAX_SIZE_BYTES` (default 50 MB, configurable via env), it is copied to `.1` (existing `.1` → `.2`, …, files beyond `LITELLM_LOG_KEEP` are dropped), then truncated in place.
- copytruncate keeps the LiteLLM process's open file descriptor valid (it continues writing to the same inode), at the cost of a millisecond-level window during which a log line could be lost — acceptable for an audit tool, and the only rotation mechanism that doesn't require restarting LiteLLM.
- To force a rotation now (regardless of size): `python3 openclaw-audit.py --rotate-litellm-logs`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LITELLM_LOG_MAX_SIZE_BYTES` | `52428800` (50 MB) | Rotate a log file when its size exceeds this |
| `LITELLM_LOG_KEEP` | `5` | Number of rotated backups to keep (`.{1..keep}`) |

## Notes

- The tool reads local logs and local SQLite state. It does not send data anywhere by itself.
- Session inspection depends on the local `openclaw sessions --json --active 1440` command working in your environment.
- The current report strings are mostly Chinese because the tool was originally written for the author's own setup.

## License

MIT. See [LICENSE](LICENSE).
