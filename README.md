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

## Environment Variables

All paths are overrideable so the repo stays portable and does not depend on one machine layout.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENCLAW_HOME` | `~/.openclaw` | OpenClaw home directory |
| `OPENCLAW_LOG_DIR` | `/tmp/openclaw` | Directory containing `openclaw-*.log` |
| `OPENCLAW_GATEWAY_LOG` | auto-detect | Gateway log path |
| `LITELLM_DIR` | `~/litellm` | LiteLLM log directory |
| `OPENCLAW_AUDIT_TZ` | `+07:00` | Audit timezone offset or `UTC` |
| `OPENCLAW_NODE` | `node` | Node.js executable |
| `OPENCLAW_CLI` | `openclaw` | OpenClaw CLI executable |

Copy the example file if you want a local env file:

```bash
cp .env.example .env
```

## Notes

- The tool reads local logs and local SQLite state. It does not send data anywhere by itself.
- Session inspection depends on the local `openclaw sessions --json --active 1440` command working in your environment.
- The current report strings are mostly Chinese because the tool was originally written for the author's own setup.

## License

MIT. See [LICENSE](LICENSE).
