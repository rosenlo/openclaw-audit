"""External state queries: OpenClaw CLI sessions and the SQLite state DB."""

import json
import os
import shutil
import sqlite3
import sys

from .config import SQLITE_DB
from .util import _fmt_ts, _session_id_from_key


# ─── Session 查询 ────────────────────────────────────────────────────
def _resolve_node_cli():
    """Resolve node and openclaw CLI paths.

    Priority: explicit env vars > PATH lookup > common absolute fallbacks.
    """
    node = os.environ.get("OPENCLAW_NODE")
    cli = os.environ.get("OPENCLAW_CLI")

    if not node:
        node = shutil.which("node")
    if not cli:
        cli = shutil.which("openclaw")

    if not node:
        for candidate in [
            "/opt/homebrew/bin/node",
            "/usr/local/bin/node",
        ]:
            if os.path.exists(candidate):
                node = candidate
                break

    if not cli:
        for candidate in [
            "/opt/homebrew/bin/openclaw",
            "/usr/local/bin/openclaw",
        ]:
            if os.path.exists(candidate):
                cli = candidate
                break

    # Fallback: if openclaw CLI is not found, use the .mjs directly
    if not cli or not os.path.exists(cli):
        for candidate in [
            os.path.expanduser("~/.openclaw/node_modules/openclaw/openclaw.mjs"),
            "/opt/homebrew/lib/node_modules/openclaw/openclaw.mjs",
            "/usr/local/lib/node_modules/openclaw/openclaw.mjs",
        ]:
            if os.path.exists(candidate):
                cli = candidate
                break

    node = node or "node"
    cli = cli or "openclaw"
    return node, cli


def query_sessions():
    """Query active sessions via openclaw CLI for context usage."""
    sessions_info = {"active": []}
    try:
        import subprocess
        node, cli = _resolve_node_cli()
        home = os.path.expanduser("~")
        env = os.environ.copy()
        env["HOME"] = home

        # Determine command: if cli ends with .mjs, run as node script; otherwise use as CLI
        if cli.endswith(".mjs") or cli.endswith(".js"):
            cmd = [node, cli, "sessions", "--json", "--active", "1440"]
            cmd_env = env
        else:
            cli_dir = os.path.dirname(cli)
            if cli_dir:
                env["PATH"] = cli_dir + ":" + env.get("PATH", "/usr/bin:/bin")
            cmd = [cli, "sessions", "--json", "--active", "1440"]
            cmd_env = env

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, env=cmd_env
        )
        if result.returncode != 0:
            sessions_info["error"] = f"exit={result.returncode} stderr={result.stderr[:200]}"
            return sessions_info
        if not result.stdout.strip():
            sessions_info["error"] = "empty output"
            return sessions_info
        d = json.loads(result.stdout)
        sessions = d.get("sessions", [])
        for s in sessions:
            total = s.get("totalTokens")
            ctx = s.get("contextTokens") or 1
            # totalTokens can be null for active/in-progress sessions
            has_tokens = total is not None and total > 0
            pct = round(total / ctx * 100, 1) if has_tokens else None
            entry = {
                "kind": s.get("kind", "?"),
                "agent": s.get("agentId", "?"),
                "model": s.get("model", "?"),
                "totalTokens": total if has_tokens else None,
                "contextTokens": ctx,
                "usagePct": pct,
                "hasTokens": has_tokens,
                "isFailed": total is None and s.get("kind") == "spawn-child",
                # Preserve the full key so the UI can derive the session id later.
                "key": s.get("key", ""),
                "sessionId": _session_id_from_key(s.get("key", "")),
                "updatedAt": s.get("updatedAt"),
                "clientUpdatedAt": _fmt_ts(s.get("updatedAt")),
            }
            sessions_info["active"].append(entry)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        sessions_info["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return sessions_info


# ─── SQLite 查询 ────────────────────────────────────────────────────
def query_sqlite():
    db_info = {}
    if not os.path.exists(SQLITE_DB):
        return db_info
    conn = sqlite3.connect(SQLITE_DB)
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT count(*), printf('%.1f', avg((ended_at-started_at)/1000.0)),
                   printf('%.1f', max((ended_at-started_at)/1000.0))
            FROM subagent_runs
            WHERE started_at IS NOT NULL AND ended_at IS NOT NULL
        """)
        row = cur.fetchone()
        if row and row[0] > 0:
            db_info["subagent_count"] = row[0]
            db_info["subagent_avg_dur"] = float(row[1])
            db_info["subagent_max_dur"] = float(row[2])

        cur.execute("SELECT status, count(*) FROM flow_runs GROUP BY status")
        flows = cur.fetchall()
        if flows:
            db_info["flows"] = dict(flows)

        cur.execute("SELECT count(*), status FROM task_runs GROUP BY status")
        tasks = cur.fetchall()
        if tasks:
            db_info["tasks"] = {r[1]: r[0] for r in tasks}

        cur.execute("""
            SELECT status, count(*) FROM channel_ingress_events
            WHERE channel_id = 'telegram' GROUP BY status
        """)
        ingress = cur.fetchall()
        if ingress:
            db_info["ingress"] = dict(ingress)
    except (sqlite3.Error, OSError) as e:
        print(f"  SQLite read error: {e}", file=sys.stderr)
    finally:
        conn.close()
    return db_info
