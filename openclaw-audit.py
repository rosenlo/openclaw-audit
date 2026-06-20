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
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta

from openclaw_audit import (
    HTML_TEMPLATE, analyze, build_root_cause_summary, build_suggestions,
    now_local, parse_litellm_err_log, parse_openclaw_logs_since,
    print_report, query_sessions, query_sqlite,
)
from openclaw_audit.config import LOCAL_TZ


# ─── CLI ────────────────────────────────────────────────────────────
def cli_mode(args):
    since = None
    since_label = ""

    if args.since:
        if args.since.endswith("h"):
            hours = int(args.since[:-1])
            since = now_local() - timedelta(hours=hours)
            since_label = f"最近 {hours} 小时"
        elif args.since.endswith("d"):
            days = int(args.since[:-1])
            since = now_local() - timedelta(days=days)
            since_label = f"最近 {days} 天"
        else:
            try:
                since = datetime.strptime(args.since, "%Y-%m-%d")
                since = since.replace(tzinfo=LOCAL_TZ)
                since_label = since.strftime("%Y-%m-%d")
            except ValueError:
                print(f"Invalid --since: {args.since}"); sys.exit(1)
    else:
        since = now_local() - timedelta(hours=1)
        since_label = "最近 1 小时"

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
    port = args.port or 9090
    host = args.host or "127.0.0.1"

    try:
        from flask import Flask, jsonify, render_template_string, request
    except ImportError:
        print("Flask required. pip install flask", file=sys.stderr)
        sys.exit(1)

    app = Flask(__name__)

    def get_data(since_param):
        since = None
        if since_param == "1h": since = now_local() - timedelta(hours=1)
        elif since_param == "3h": since = now_local() - timedelta(hours=3)
        elif since_param == "6h": since = now_local() - timedelta(hours=6)
        elif since_param == "24h": since = now_local() - timedelta(hours=24)
        elif since_param == "today": since = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
        elif since_param == "yesterday": since = now_local().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        else: since = now_local() - timedelta(hours=1)

        entries = parse_openclaw_logs_since(since)
        litellm_entries = parse_litellm_err_log(since)
        entries.extend(litellm_entries)
        entries.sort(key=lambda x: x[1])
        result = analyze(entries, since)
        result["sessions"] = query_sessions()
        return result

    @app.route("/")
    def index():
        since_param = request.args.get("since", "1h")
        data = get_data(since_param)
        suggestions = [build_root_cause_summary(data)] + build_suggestions(data, data["telegram"])

        sqlite_info = query_sqlite()
        data["sqlite"] = sqlite_info

        return render_template_string(HTML_TEMPLATE,
            data=data, suggestions=suggestions, sel=since_param)

    @app.route("/api/data")
    def api_data():
        since_param = request.args.get("since", "1h")
        data = get_data(since_param)
        data["sqlite"] = query_sqlite()
        return jsonify(data)

    print(f"  Web dashboard at http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


# ─── Watch模式 ──────────────────────────────────────────────────────
def watch_mode(args):
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
        """)
    parser.add_argument("--since", default="", help="1h, 3h, 6h, 24h, today, YYYY-MM-DD (default: 1h)")
    parser.add_argument("--web", action="store_true", help="Web Dashboard")
    parser.add_argument("--port", type=int, default=9090, help="Web port (default: 9090)")
    parser.add_argument("--host", default="127.0.0.1", help="Web bind (default: 127.0.0.1)")
    parser.add_argument("--watch", action="store_true", help="Watch mode")
    parser.add_argument("--interval", type=int, default=30, help="Watch interval (default: 30)")
    parser.add_argument("--hours", type=int, default=1, help="Watch window (default: 1)")

    args = parser.parse_args()

    if args.web:
        web_mode(args)
    elif args.watch:
        watch_mode(args)
    else:
        cli_mode(args)


if __name__ == "__main__":
    main()
