#!/usr/bin/env python3
"""
OpenClaw Audit Tool
===================
Telegram Bot → OpenClaw → LLM (litellm) 调用链路审计工具

Usage:
  python3 openclaw-audit.py                       # CLI 模式：最近1小时
  python3 openclaw-audit.py --since 24h           # 最近24小时
  python3 openclaw-audit.py --since 2026-06-18    # 指定日期
  python3 openclaw-audit.py --web                 # Web Dashboard (端口 9090)
  python3 openclaw-audit.py --watch               # 持续观察模式

Environment Variables:
  OPENCLAW_HOME       OpenClaw 安装目录 (default: ~/.openclaw)
  OPENCLAW_LOG_DIR    OpenClaw 日志目录 (default: /tmp/openclaw)
  OPENCLAW_GATEWAY_LOG  Gateway 日志路径 (default: 自动探测)
  LITELLM_DIR         LiteLLM 日志目录 (default: ~/litellm)
  OPENCLAW_AUDIT_TZ   时区偏移 (default: +07:00, 或 UTC)
  OPENCLAW_NODE       Node.js 可执行路径 (default: node)
  OPENCLAW_CLI        OpenClaw CLI 路径 (default: openclaw)

LiteLLM 日志格式:
  推荐 LiteLLM 进程设 JSON_LOGS=true，让 err.log 每行一个 JSON 对象、
  timestamp 带完整日期年份 (LITELLM_LOG=DEBUG 控制级别)。本工具只解析
  JSON 行;老的 HH:MM:SS 文本格式行会被跳过 (没有日期,会让历史事件
  漂移到未来)。注意是 JSON_LOGS，不是 LITELLM_LOG=JSON。

  launchd 不轮转 err.log/out.log,本工具启动时按 LITELLM_LOG_MAX_SIZE_BYTES
  (默认 50MB) 自动 copytruncate 轮转,保留 LITELLM_LOG_KEEP (默认 5) 份。
  也可用 --rotate-litellm-logs 手动触发。
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta

from openclaw_audit import (
    HTML_TEMPLATE, analyze, build_root_cause_summary, build_suggestions,
    maybe_rotate_litellm_logs, now_local,
    parse_litellm_err_log, parse_openclaw_logs_since,
    print_report, query_sessions, query_sqlite,
    rotate_litellm_logs,
)
from openclaw_audit.config import LOCAL_TZ
from openclaw_audit.util import parse_since_arg, tz_offset_str


def _run_startup_rotation():
    """Rotate litellm logs if they exceed the size threshold. Called once
    at CLI/web/watch startup so the log files don't grow unbounded
    (launchd redirects litellm stdout/stderr but never rotates them)."""
    results = maybe_rotate_litellm_logs()
    for path, r in results.items():
        if r["rotated"]:
            print(f"  ♻️  轮转 {os.path.basename(path)}: {r['reason']}", file=sys.stderr)


# ─── CLI ────────────────────────────────────────────────────────────
def cli_mode(args):
    _run_startup_rotation()
    try:
        since, since_label = parse_since_arg(args.since)
    except ValueError:
        print(f"Invalid --since: {args.since}"); sys.exit(1)

    print(f"  分析区间:  {since_label}", file=sys.stderr)

    # Parse OpenClaw logs
    print("  🔍 解析 OpenClaw 日志...", file=sys.stderr)
    entries = parse_openclaw_logs_since(since)

    # Parse Litellm logs
    print("  🔍 解析 Litellm 日志...", file=sys.stderr)
    litellm_entries = parse_litellm_err_log(since)
    entries.extend(litellm_entries)
    entries.sort(key=lambda x: x[1])

    print(f"  📄 OpenClaw: {len(entries) - len(litellm_entries)} 条, Litellm: {len(litellm_entries)} 条", file=sys.stderr)

    if not entries:
        print("  ⚠️  无匹配日志", file=sys.stderr)
        return

    result = analyze(entries, since)
    sqlite_info = query_sqlite()
    sessions_info = query_sessions()
    print_report(result, sqlite_info, sessions_info)


# ─── Web ────────────────────────────────────────────────────────────
def web_mode(args):
    _run_startup_rotation()
    port = args.port or 9090
    host = args.host or "127.0.0.1"

    try:
        from flask import Flask, jsonify, render_template_string, request
    except ImportError:
        print("Flask required. pip install flask", file=sys.stderr)
        sys.exit(1)

    app = Flask(__name__)

    # Per-window caches: since_param -> (wall_clock_s, payload).
    # parsing + analyze + query_sessions (a 15s-timeout subprocess) on every
    # dashboard refresh is wasteful when the user is just clicking between
    # 1h/3h/24h tabs; a short TTL reuses the computed payload.
    _DATA_CACHE_TTL = 5.0
    _data_cache = {}
    _sqlite_cache = {"ts": 0.0, "value": None}

    def _generated_at_str():
        return (
            now_local().strftime("%Y-%m-%d %H:%M:%S")
            + " (" + tz_offset_str(LOCAL_TZ) + ")"
        )

    def _since_for_param(since_param):
        if since_param == "1h": return now_local() - timedelta(hours=1)
        if since_param == "3h": return now_local() - timedelta(hours=3)
        if since_param == "6h": return now_local() - timedelta(hours=6)
        if since_param == "24h": return now_local() - timedelta(hours=24)
        if since_param == "today":
            return now_local().replace(hour=0, minute=0, second=0, microsecond=0)
        if since_param == "yesterday":
            return now_local().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        return now_local() - timedelta(hours=1)

    def get_data(since_param):
        """Return (result, error_str). On error, result is None and error_str
        is a short message for the dashboard banner. The cached payload is
        returned by reference but must not be mutated by callers — sqlite is
        carried separately to avoid mutating the cached result."""
        now = time.time()
        cached = _data_cache.get(since_param)
        if cached and now - cached[0] < _DATA_CACHE_TTL:
            return cached[1], None

        try:
            since = _since_for_param(since_param)
            entries = parse_openclaw_logs_since(since)
            litellm_entries = parse_litellm_err_log(since)
            entries.extend(litellm_entries)
            entries.sort(key=lambda x: x[1])
            result = analyze(entries, since)
            result["sessions"] = query_sessions()
            result["_generated_at"] = _generated_at_str()
            _data_cache[since_param] = (now, result)
            return result, None
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:200]}"

    def get_sqlite():
        """SQLite info with its own short TTL — separate from get_data so
        sqlite reads don't force a full data recompute, and so the cached
        result payload isn't mutated."""
        now = time.time()
        if _sqlite_cache["value"] is not None and now - _sqlite_cache["ts"] < _DATA_CACHE_TTL:
            return _sqlite_cache["value"]
        try:
            value = query_sqlite()
            _sqlite_cache["ts"] = now
            _sqlite_cache["value"] = value
            return value
        except Exception as e:
            return {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    def _build_payload(since_param):
        """Build the full payload shared by / and /api/fragments."""
        data, err = get_data(since_param)
        if data is None:
            return {"error": err, "generated_at": _generated_at_str()}
        suggestions = [build_root_cause_summary(data)] + build_suggestions(data, data["telegram"])
        sessions_info = data.get("sessions", {"active": []})
        from openclaw_audit.render import render_section_fragments
        fragments = render_section_fragments(data, suggestions, sessions_info)
        return {
            **fragments,
            "generated_at": data.get("_generated_at", _generated_at_str()),
            "error": None,
        }

    @app.route("/")
    def index():
        since_param = request.args.get("since", "1h")
        payload = _build_payload(since_param)
        # sqlite is shown in CLI only; web keeps it in /api/data for programmatic access
        return render_template_string(
            HTML_TEMPLATE,
            sel=since_param,
            generated_at=payload["generated_at"],
            stats_openclaw=payload.get("stats-openclaw", ""),
            stats_litellm=payload.get("stats-litellm", ""),
            stats_latency=payload.get("stats-latency", ""),
            stats_sessions=payload.get("stats-sessions", ""),
            time_series_html=payload.get("time-series", ""),
            suggestions_html=payload.get("suggestions", ""),
            events_html=payload.get("event-list", ""),
        )

    @app.route("/api/fragments")
    def api_fragments():
        """Return each dashboard section as a pre-rendered HTML string, plus
        a generated_at timestamp and error field. JS fetches this and
        patches container innerHTML — no full page reload."""
        since_param = request.args.get("since", "1h")
        return jsonify(_build_payload(since_param))

    @app.route("/api/data")
    def api_data():
        """Raw JSON view of the audit result + sqlite (programmatic use)."""
        since_param = request.args.get("since", "1h")
        data, err = get_data(since_param)
        if data is None:
            return jsonify({"error": err})
        # Strip non-JSON-serializable fields
        data_copy = {k: v for k, v in data.items() if k != "_generated_at"}
        data_copy["sqlite"] = get_sqlite()
        data_copy["generated_at"] = data.get("_generated_at", _generated_at_str())
        return jsonify(data_copy)

    print(f"  Web dashboard at http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)


# ─── Watch模式 ──────────────────────────────────────────────────────
def watch_mode(args):
    _run_startup_rotation()
    from openclaw_audit.util import DIM

    interval = args.interval or 30
    print(f"  Watch mode (every {interval}s, Ctrl+C to exit)", file=sys.stderr)
    try:
        while True:
            since = now_local() - timedelta(hours=args.hours or 1)
            entries = parse_openclaw_logs_since(since)
            entries.extend(parse_litellm_err_log(since))
            entries.sort(key=lambda x: x[1])
            result = analyze(entries, since)
            sqlite_info = query_sqlite()
            sessions_info = query_sessions()
            os.system("clear" if sys.platform != "win32" else "cls")
            print_report(result, sqlite_info, sessions_info)
            print(f"  {DIM(f'Next refresh in {interval}s (Ctrl+C)')}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("  Exited", file=sys.stderr)


# ─── 入口 ────────────────────────────────────────────────────────────
def rotate_mode(args):
    """Force-rotate litellm logs and exit."""
    results = rotate_litellm_logs()
    for path, r in results.items():
        basename = os.path.basename(path)
        if r["rotated"]:
            print(f"  ♻️  轮转 {basename}: {r['reason']}")
        else:
            print(f"  ⏭️  跳过 {basename}: {r['reason']}")


def main():
    parser = argparse.ArgumentParser(
        description="OpenClaw + LiteLLM Audit Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 %(prog)s                          # 最近1小时
  python3 %(prog)s --since 6h               # 最近6小时
  python3 %(prog)s --since today            # 今天
  python3 %(prog)s --web                    # Web Dashboard
  python3 %(prog)s --watch                  # 持续观察
  python3 %(prog)s --rotate-litellm-logs    # 手动轮转 litellm 日志
        """)
    parser.add_argument("--since", default="", help="1h, 3h, 6h, 24h, today, YYYY-MM-DD (default: 1h)")
    parser.add_argument("--web", action="store_true", help="Web Dashboard")
    parser.add_argument("--port", type=int, default=9090, help="Web port (default: 9090)")
    parser.add_argument("--host", default="127.0.0.1", help="Web bind (default: 127.0.0.1)")
    parser.add_argument("--watch", action="store_true", help="Watch mode")
    parser.add_argument("--interval", type=int, default=30, help="Watch interval (default: 30)")
    parser.add_argument("--hours", type=int, default=1, help="Watch window (default: 1)")
    parser.add_argument(
        "--rotate-litellm-logs", action="store_true",
        help="Force-rotate litellm err/out logs (copytruncate) and exit",
    )

    args = parser.parse_args()

    if args.rotate_litellm_logs:
        rotate_mode(args)
    elif args.web:
        web_mode(args)
    elif args.watch:
        watch_mode(args)
    else:
        cli_mode(args)


if __name__ == "__main__":
    main()
