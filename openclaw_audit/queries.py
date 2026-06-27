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
    """Query active sessions via openclaw CLI for context usage and state."""
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
                # ── State markers (new): surface what the session is doing
                # right now, not just token usage. These come straight from
                # `openclaw sessions --json` and are interpreted by the
                # render layer into status badges / suggestions.
                "abortedLastRun": bool(s.get("abortedLastRun")),
                "systemSent": bool(s.get("systemSent")),
                "tokensFresh": s.get("totalTokensFresh"),
                "ageMs": s.get("ageMs"),
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

        # Each section is independently try-guarded: a missing column or
        # table on an older OpenClaw install must not block later sections
        # (subagent_runs schema, task_runs, diagnostic_events all evolved
        # over versions). The outer try only catches the truly unexpected.

        try:
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
        except sqlite3.Error:
            pass

        try:
            cur.execute("SELECT status, count(*) FROM flow_runs GROUP BY status")
            flows = cur.fetchall()
            if flows:
                db_info["flows"] = dict(flows)
        except sqlite3.Error:
            pass

        try:
            cur.execute("SELECT count(*), status FROM task_runs GROUP BY status")
            tasks = cur.fetchall()
            if tasks:
                db_info["tasks"] = {r[1]: r[0] for r in tasks}
        except sqlite3.Error:
            pass

        try:
            cur.execute("""
                SELECT status, count(*) FROM channel_ingress_events
                WHERE channel_id = 'telegram' GROUP BY status
            """)
            ingress = cur.fetchall()
            if ingress:
                db_info["ingress"] = dict(ingress)
        except sqlite3.Error:
            pass

        # ── Recent failed/lost task_runs: surface the error text so the
        # operator can see FailoverError / "No API key" / "Codex
        # subscription usage limit" without reading raw logs. Cap at 5
        # rows so the report stays readable; each row is (status, err,
        # age_min, task_label).
        try:
            cur.execute("""
                SELECT status, error,
                       (strftime('%s','now') - ended_at/1000.0) / 60.0,
                       label
                FROM task_runs
                WHERE status IN ('failed', 'lost') AND ended_at IS NOT NULL
                ORDER BY ended_at DESC LIMIT 5
            """)
            rows = cur.fetchall()
            if rows:
                db_info["recent_task_failures"] = [
                    {
                        "status": r[0],
                        "error": _truncate_sql_text(r[1], 200),
                        "age_min": round(r[2], 1) if r[2] is not None else None,
                        "label": _truncate_sql_text(r[3], 80),
                    }
                    for r in rows
                ]
        except sqlite3.Error:
            # schema drift on older installs — task_runs may not have
            # these columns or the table may be absent entirely.
            pass

        # ── subagent_runs announce give-up: surface runs whose last
        # announce delivery failed. These are the "wedged parent" events
        # — the child completed but the parent never received the result
        # because the announce retry-limit was hit.
        try:
            cur.execute("""
                SELECT substr(run_id, 1, 8),
                       last_announce_delivery_error,
                       announce_retry_count,
                       ended_reason,
                       (strftime('%s','now') - ended_at/1000.0) / 60.0
                FROM subagent_runs
                WHERE last_announce_delivery_error IS NOT NULL
                  AND last_announce_delivery_error != ''
                  AND ended_at IS NOT NULL
                ORDER BY ended_at DESC LIMIT 5
            """)
            rows = cur.fetchall()
            if rows:
                db_info["recent_announce_failures"] = [
                    {
                        "run_id": r[0],
                        "error": _truncate_sql_text(r[1], 200),
                        "retries": r[2],
                        "ended_reason": r[3],
                        "age_min": round(r[4], 1) if r[4] is not None else None,
                    }
                    for r in rows
                ]
        except sqlite3.Error:
            pass

        # ── diagnostic_events: long-running / stalled session warnings.
        # openclaw emits these every ~5 minutes for sessions stuck in
        # state=processing. Each (scope, event_key) is upserted, so we
        # only get the latest snapshot per key — still useful to spot
        # currently-stuck sessions at audit time.
        try:
            cur.execute("""
                SELECT scope, event_key, payload_json,
                       (strftime('%s','now') - created_at/1000.0) / 60.0
                FROM diagnostic_events
                WHERE created_at > strftime('%s','now') * 1000 - 3600 * 1000
                ORDER BY created_at DESC LIMIT 10
            """)
            rows = cur.fetchall()
            if rows:
                db_info["recent_diagnostics"] = [
                    {
                        "scope": r[0],
                        "event_key": r[1],
                        "payload": _truncate_sql_text(r[2], 200),
                        "age_min": round(r[3], 1) if r[3] is not None else None,
                    }
                    for r in rows
                ]
        except sqlite3.Error:
            pass
    except (sqlite3.Error, OSError) as e:
        print(f"  SQLite read error: {e}", file=sys.stderr)
    finally:
        conn.close()
    return db_info


def _truncate_sql_text(s, n=200):
    """Truncate a SQLite text field for display. None → empty string."""
    if s is None:
        return ""
    s = str(s)
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"
