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

import json, sys, os, sqlite3, glob, time, argparse, re, shutil
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict

# ─── 路径配置 ───────────────────────────────────────────────────────
# 所有路径均可通过环境变量覆盖，便于公开仓库使用。
# 环境变量不存在时使用通用 fallback（不暴露特定用户路径）。
OPENCLAW_HOME = os.environ.get("OPENCLAW_HOME", "~/.openclaw")
OPENCLAW_HOME = os.path.expanduser(OPENCLAW_HOME)

LOG_DIR = os.environ.get("OPENCLAW_LOG_DIR", "/tmp/openclaw")

GATEWAY_LOG = os.environ.get("OPENCLAW_GATEWAY_LOG", "")
if not GATEWAY_LOG:
    # 尝试常见位置，都不存在也不报错
    for _candidate in [
        os.path.expanduser("~/Library/Logs/openclaw/gateway.log"),
        os.path.expanduser("~/.openclaw/gateway.log"),
        "/var/log/openclaw/gateway.log",
    ]:
        if os.path.exists(_candidate):
            GATEWAY_LOG = _candidate
            break

SQLITE_DB = os.path.join(OPENCLAW_HOME, "state", "openclaw.sqlite")

LITELLM_DIR = os.environ.get("LITELLM_DIR", "~/litellm")
LITELLM_DIR = os.path.expanduser(LITELLM_DIR)

LITELLM_OUT_LOG = os.path.join(LITELLM_DIR, "litellm.out.log")
LITELLM_ERR_LOG = os.path.join(LITELLM_DIR, "litellm.err.log")

# ─── 时区 ───────────────────────────────────────────────────────────
# 可通过 OPENCLAW_AUDIT_TZ 环境变量覆盖，格式: +07:00 / -05:00 / UTC
_AUDIT_TZ_STR = os.environ.get("OPENCLAW_AUDIT_TZ", "+07:00")
if _AUDIT_TZ_STR.upper() == "UTC":
    LOCAL_TZ = timezone.utc
else:
    LOCAL_TZ = timezone(timedelta(hours=int(_AUDIT_TZ_STR[:3])))

def now_local():
    return datetime.now(LOCAL_TZ)

TODAY = now_local().strftime("%Y-%m-%d")

def parse_ts(ts_str):
    """Parse OpenClaw ISO timestamp or litellm HH:MM:SS timestamp."""
    if not ts_str:
        return None
    try:
        # OpenClaw format: 2026-06-18T07:43:06.757+07:00
        if "T" in ts_str or " " in ts_str:
            if "+" in ts_str:
                ts_clean = ts_str.rsplit("+", 1)[0]
                fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in ts_clean else "%Y-%m-%dT%H:%M:%S"
                dt = datetime.strptime(ts_clean, fmt)
                dt = dt.replace(tzinfo=LOCAL_TZ)
                return dt.astimezone(LOCAL_TZ)
            elif "Z" in ts_str:
                ts_clean = ts_str.replace("Z", "")
                fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in ts_clean else "%Y-%m-%dT%H:%M:%S"
                dt = datetime.strptime(ts_clean, fmt)
                dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(LOCAL_TZ)
            else:
                fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in ts_str else "%Y-%m-%dT%H:%M:%S"
                return datetime.strptime(ts_str, fmt)
        # litellm format: HH:MM:SS
        elif re.match(r"^\d{2}:\d{2}:\d{2}", ts_str):
            today_str = TODAY
            dt = datetime.strptime(f"{today_str} {ts_str}", "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=LOCAL_TZ)
            return dt
    except (ValueError, IndexError):
        return None
    return None


def _fmt_ts(ts):
    """Convert a millisecond epoch or ISO string to a localized time string (HH:MM)."""
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        try:
            dt = datetime.fromtimestamp(ts / 1000.0, tz=LOCAL_TZ)
            return dt.strftime("%H:%M")
        except (ValueError, OSError):
            return ""
    if isinstance(ts, str):
        pt = parse_ts(ts)
        if pt:
            return pt.strftime("%H:%M")
    return ""



def fmt_duration(sec):
    if sec is None:
        return "N/A"
    if sec < 1:
        return f"{sec*1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(sec), 60)
    return f"{m}m{s}s"


# ─── OpenClaw 日志解析 ─────────────────────────────────────────────
def parse_openclaw_log(filepath):
    """Parse an OpenClaw JSON log file."""
    entries = []
    if not os.path.exists(filepath):
        return entries
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                level = d.get("logLevelName", "")
                msg = d.get("message", "")
                if not msg:
                    try:
                        inner = json.loads(d.get("0", "{}"))
                        msg = inner.get("1", "")
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass
                ts = d.get("time", "")
                if ts:
                    entries.append(("openclaw", ts, level, msg))
    except (IOError, OSError) as e:
        print(f"Warning: cannot read {filepath}: {e}", file=sys.stderr)
    return entries


def parse_openclaw_logs_since(since=None):
    """Parse all OpenClaw log files matching the date filter."""
    all_entries = []
    pattern = os.path.join(LOG_DIR, "openclaw-*.log")
    for f in sorted(glob.glob(pattern)):
        if since:
            try:
                fdate_str = f.split("openclaw-")[1].split(".log")[0]
                fdate = datetime.strptime(fdate_str, "%Y-%m-%d")
                if since.date() and fdate.date() < since.date():
                    continue
            except (ValueError, IndexError):
                pass
        all_entries.extend(parse_openclaw_log(f))
    all_entries.sort(key=lambda x: x[1])
    return all_entries


# ─── LiteLLM 日志解析 ──────────────────────────────────────────────
def parse_litellm_err_log(since=None):
    """Parse litellm.err.log: lines have prefix like 09:11:20 - LiteLLM Router:ERROR: ..."""
    entries = []
    if not os.path.exists(LITELLM_ERR_LOG):
        return entries

    since_ts = None
    if since:
        since_ts = since.timestamp()

    ansi_pat = re.compile(r"\033\[[0-9;]*m")
    line_pat = re.compile(
        r"^(\d{2}:\d{2}:\d{2})\s*-\s*LiteLLM\s+(\S+):(\S+):\s+(.*)$"
    )

    try:
        with open(LITELLM_ERR_LOG) as f:
            for line in f:
                clean = ansi_pat.sub("", line).strip()
                m = line_pat.match(clean)
                if m:
                    ts_str = m.group(1)
                    component = m.group(2)
                    level = m.group(3)
                    msg = m.group(4)

                    parsed = parse_ts(ts_str)
                    if parsed and since_ts is not None and parsed.timestamp() < since_ts:
                        continue

                    full_ts = f"{TODAY}T{ts_str}+07:00"
                    entries.append(("litellm", full_ts, level, f"[{component}] {msg}"))
    except (IOError, OSError) as e:
        print(f"Warning: cannot read litellm err: {e}", file=sys.stderr)

    return entries


def parse_litellm_out_log(since=None):
    """Parse litellm.out.log (Uvicorn access log + verbose debug)."""
    result = {
        "total_requests": 0,
        "status_codes": Counter(),
        "streaming_responses": 0,
    }
    if not os.path.exists(LITELLM_OUT_LOG):
        return result

    try:
        with open(LITELLM_OUT_LOG) as f:
            for line in f:
                # Uvicorn access log: INFO:     127.0.0.1:xxx - "POST /chat/completions HTTP/1.1" 200 OK
                if 'POST /chat/completions' in line and 'HTTP/1.1"' in line:
                    result["total_requests"] += 1
                    m = re.search(r'HTTP/1\.1" (\d{3})', line)
                    if m:
                        code = m.group(1)
                        result["status_codes"][code] += 1
                elif 'streaming response' in line.lower():
                    result["streaming_responses"] += 1
    except (IOError, OSError) as e:
        print(f"Warning: cannot read litellm out: {e}", file=sys.stderr)

    return result


# ─── LiteLLM 错误分类 ─────────────────────────────────────────────
def classify_litellm_entry(msg):
    ml = msg.lower()
    cat = {"type": "other"}

    # ReadTimeout / APIConnectionError (from agnes upstream)
    if "readtimeout" in ml or "timeout on reading" in ml:
        cat["type"] = "upstream_timeout"
        return cat
    if "apiconnectionerror" in ml or "midstreamfallback" in ml:
        cat["type"] = "upstream_connection"
        return cat
    if "fallback also failed" in ml:
        cat["type"] = "upstream_fallback_failed"
        return cat
    if "exception occured" in ml:
        cat["type"] = "proxy_exception"
        return cat

    # set_verbose deprecation warning
    if "set_verbose is deprecated" in ml:
        cat["type"] = "deprecation_warning"
        return cat

    # General error
    if ":error" in ml or "traceback" in ml:
        cat["type"] = "general_error"
        return cat

    return cat


# ─── OpenClaw 事件分类 ─────────────────────────────────────────────
def classify_entry(msg, level):
    ml = msg.lower()
    cat = {}

    if "inbound message" in ml and "telegram" in ml:
        cat["type"] = "telegram_in"
        try:
            chars_part = msg.rsplit(", ", 1)[-1] if ", " in msg else ""
            cat["chars"] = int(chars_part.replace(" chars)", "").replace(" chars", ""))
        except (ValueError, IndexError):
            cat["chars"] = 0
        return cat

    if "message processed" in ml and "telegram" in ml:
        cat["type"] = "telegram_out"
        if "outcome=error" in ml:
            cat["error"] = True
        for part in msg.split():
            if part.startswith("duration="):
                try:
                    cat["duration_ms"] = int(part.replace("duration=", "").replace("ms", ""))
                except ValueError:
                    pass
        return cat

    if "model-fetch" in ml and "error" in ml:
        cat["type"] = "llm_error"
        for part in msg.split():
            if part.startswith("elapsedMs="):
                try:
                    cat["elapsed_ms"] = int(part.split("=")[1])
                except (ValueError, IndexError):
                    pass
            if part.startswith("provider="):
                cat["provider"] = part.split("=")[1]
        cat["reason"] = "abort" if ("AbortError" in msg or "abort" in ml) else "error"
        return cat

    if "context overflow" in ml or "context-overflow" in ml:
        cat["type"] = "context_overflow"
        cat["subtype"] = "precheck" if "precheck" in ml else ("diagnostic" if "diag" in ml else "detected")
        for part in msg.split():
            if part.startswith("messages="):
                try:
                    cat["msg_count"] = int(part.split("=")[1])
                except (ValueError, IndexError):
                    pass
        return cat

    if "auto-compaction" in ml:
        cat["type"] = "compaction"
        if "succeeded" in ml:
            cat["subtype"] = "success"
        elif "incomplete" in ml:
            cat["subtype"] = "incomplete"
        elif "start" in ml:
            cat["subtype"] = "start"
        else:
            cat["subtype"] = "retry"
        return cat

    if "failover" in ml:
        cat["type"] = "failover"
        return cat

    if "llm request timed out" in ml or ("timed out" in ml and "llm" in ml):
        cat["type"] = "llm_timeout"
        for part in msg.split():
            if part.startswith("durationMs="):
                try:
                    cat["duration_ms"] = int(part.split("=")[1])
                except (ValueError, IndexError):
                    pass
        return cat

    if "incomplete turn" in ml:
        cat["type"] = "incomplete_turn"
        for part in msg.split():
            if part.startswith("provider="):
                cat["provider"] = part.split("=")[1]
        return cat

    if "edit failed" in ml:
        cat["type"] = "edit_failed"
        return cat

    if "read failed" in ml:
        cat["type"] = "read_failed"
        return cat

    if "config hot reload" in ml:
        cat["type"] = "config_reload"
        return cat

    if "lane task error" in ml:
        cat["type"] = "lane_error"
        for part in msg.split():
            if part.startswith("durationMs="):
                try:
                    cat["duration_ms"] = int(part.split("=")[1])
                except (ValueError, IndexError):
                    pass
        return cat

    if "fetch fallback" in ml or "closed before connect" in ml:
        cat["type"] = "telegram_conn_issue"
        return cat

    if "fetch timeout" in ml:
        cat["type"] = "fetch_timeout"
        return cat

    if "embedded run agent end" in ml:
        cat["type"] = "agent_end"
        return cat

    if level in ("ERROR", "WARN", "FATAL"):
        cat["type"] = "unknown_error"
        cat["level"] = level
        return cat

    cat["type"] = "other"
    return cat


# ─── 核心分析 ──────────────────────────────────────────────────────
NON_FATAL_LITELLM_TYPES = {"deprecation_warning", "general_error", "other"}

def analyze(entries, since=None):
    result = {
        "summary": {},
        "telegram": {"inbound": 0, "outbound": 0, "errors": 0},
        "llm": {"errors": 0, "timeouts": 0, "latencies": []},
        "context": {"overflows": 0, "compactions": {"success": 0, "incomplete": 0, "failed": 0}},
        "failovers": 0, "connection_issues": 0, "config_reloads": 0,
        "tool_errors": {"edit": 0, "read": 0},
        "incomplete_turns": 0, "other_errors": [],
        "time_series": Counter(),
        "raw_events": [],
        "litellm": {
            "total_requests": 0, "streaming_responses": 0,
            "status_codes": {},
            "upstream_timeouts": 0, "upstream_errors": 0,
            "warnings": 0,
        },
    }

    hourly_counts = Counter()
    litellm_err_counts = Counter()

    for source, ts_str, level, msg in entries:
        parsed_ts = parse_ts(ts_str)

        if "heartbeat" in msg.lower() or "health-monitor" in msg.lower():
            continue

        if parsed_ts:
            hour_key = parsed_ts.strftime("%H:00")
            hourly_counts[hour_key] += 1

        # ── OpenClaw events ──
        if source == "openclaw":
            cat = classify_entry(msg, level)
            etype = cat.get("type", "other")
            tag_emoji = "📩"
            severity = level

            if etype == "telegram_in":
                result["telegram"]["inbound"] += 1
                tag_emoji = "📩"
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "📩 Telegram消息",
                    "detail": msg[:120], "level": level
                })

            elif etype == "telegram_out":
                result["telegram"]["outbound"] += 1
                if cat.get("error"):
                    result["telegram"]["errors"] += 1

            elif etype == "llm_error":
                result["llm"]["errors"] += 1
                elapsed = cat.get("elapsed_ms", 0)
                if elapsed:
                    result["llm"]["latencies"].append(elapsed / 1000.0)
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "❌ LLM错误",
                    "detail": f"provider={cat.get('provider','?')} elapsed={fmt_duration(elapsed/1000 if elapsed else None)} reason={cat.get('reason','?')}",
                    "level": "ERROR"
                })

            elif etype == "llm_timeout":
                result["llm"]["timeouts"] += 1
                dur = cat.get("duration_ms", 0) / 1000.0
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "⏰ LLM超时",
                    "detail": f"duration={fmt_duration(dur)}", "level": "ERROR"
                })

            elif etype == "context_overflow":
                result["context"]["overflows"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "📦 上下文溢出",
                    "detail": f"subtype={cat.get('subtype','?')} msgs={cat.get('msg_count','?')}",
                    "level": "WARN"
                })

            elif etype == "compaction":
                sub = cat.get("subtype", "?")
                if sub == "success":
                    result["context"]["compactions"]["success"] += 1
                elif sub in ("incomplete", "start"):
                    result["context"]["compactions"]["incomplete"] += 1

            elif etype == "failover":
                result["failovers"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "🔄 Failover",
                    "detail": msg[:100], "level": "WARN"
                })

            elif etype == "incomplete_turn":
                result["incomplete_turns"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "⚠️ 不完整响应",
                    "detail": f"provider={cat.get('provider','?')}", "level": "WARN"
                })

            elif etype == "telegram_conn_issue":
                result["connection_issues"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "🔌 Telegram断连",
                    "detail": msg[:100], "level": "WARN"
                })

            elif etype == "config_reload":
                result["config_reloads"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "⚙️ 配置热加载",
                    "detail": msg[:120], "level": "INFO"
                })

            elif etype == "edit_failed":
                result["tool_errors"]["edit"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "✏️ Edit失败",
                    "detail": msg[:120], "level": "WARN"
                })

            elif etype == "read_failed":
                result["tool_errors"]["read"] += 1

            elif etype in ("lane_error", "fetch_timeout", "agent_end"):
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": f"⚠️ {cat.get('type','?')}",
                    "detail": msg[:120],
                    "level": "WARN" if etype != "agent_end" else "INFO"
                })

            elif etype == "unknown_error":
                result["other_errors"].append(msg[:150])
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": f"❌ {level}",
                    "detail": msg[:150], "level": level
                })

        # ── LiteLLM events ──
        elif source == "litellm":
            cat = classify_litellm_entry(msg)
            etype = cat.get("type", "other")

            if etype == "upstream_timeout":
                result["litellm"]["upstream_timeouts"] += 1
                litellm_err_counts["upstream_timeout"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "⏰ Litellm上游超时",
                    "detail": msg[:120], "level": "ERROR"
                })

            elif etype == "upstream_connection":
                result["litellm"]["upstream_errors"] += 1
                litellm_err_counts["upstream_connection"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "🔌 Litellm连接错误",
                    "detail": msg[:120], "level": "ERROR"
                })

            elif etype == "upstream_fallback_failed":
                result["litellm"]["upstream_errors"] += 1
                litellm_err_counts["fallback_failed"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "❌ Litellm Fallback失败",
                    "detail": msg[:120], "level": "ERROR"
                })

            elif etype == "proxy_exception":
                result["litellm"]["upstream_errors"] += 1
                litellm_err_counts["proxy_exception"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "❌ Litellm代理异常",
                    "detail": msg[:120], "level": "ERROR"
                })

            elif etype == "deprecation_warning":
                result["litellm"]["warnings"] += 1

            elif etype == "general_error":
                result["litellm"]["upstream_errors"] += 1

    # ── Also read litellm.out log for request stats ──
    litellm_out = parse_litellm_out_log(since)
    result["litellm"]["total_requests"] = litellm_out["total_requests"]
    result["litellm"]["streaming_responses"] = litellm_out["streaming_responses"]
    result["litellm"]["status_codes"] = dict(litellm_out["status_codes"])

    # ── Summary ──
    total_events = len(entries)
    result["summary"] = {
        "total_events": total_events,
        "telegram_in": result["telegram"]["inbound"],
        "telegram_out": result["telegram"]["outbound"],
        "llm_errors": result["llm"]["errors"],
        "llm_timeouts": result["llm"]["timeouts"],
        "failovers": result["failovers"],
        "context_overflows": result["context"]["overflows"],
        "compaction_success": result["context"]["compactions"]["success"],
        "incomplete_turns": result["incomplete_turns"],
        "connection_issues": result["connection_issues"],
        "config_reloads": result["config_reloads"],
        "edit_fails": result["tool_errors"]["edit"],
        "avg_llm_latency": None,
        "litellm_requests": result["litellm"]["total_requests"],
        "litellm_upstream_timeouts": result["litellm"]["upstream_timeouts"],
        "litellm_upstream_errors": result["litellm"]["upstream_errors"],
    }

    if result["llm"]["latencies"]:
        lats = sorted(result["llm"]["latencies"])
        result["summary"]["avg_llm_latency"] = sum(lats) / len(lats)
        result["summary"]["max_llm_latency"] = max(lats)
        idx95 = int(len(lats) * 0.95)
        result["summary"]["p95_llm_latency"] = lats[min(idx95, len(lats)-1)]

    result["time_series"] = dict(sorted(hourly_counts.items()))
    result["raw_events"].reverse()

    return result


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
            "/opt/homebrew/Cellar/node@22/22.22.3/bin/node",
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
                "key": s.get("key", "")[-50:],
                "sessionId": s.get("sessionId", ""),
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
    try:
        conn = sqlite3.connect(SQLITE_DB)
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

        conn.close()
    except (sqlite3.Error, OSError) as e:
        print(f"  SQLite read error: {e}", file=sys.stderr)
    return db_info


# ─── CLI 格式化 ─────────────────────────────────────────────────────
def color(text, code):
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text

GREEN  = lambda t: color(t, "92")
RED    = lambda t: color(t, "91")
YELLOW = lambda t: color(t, "93")
CYAN   = lambda t: color(t, "96")
BOLD   = lambda t: color(t, "1")
DIM    = lambda t: color(t, "90")


def print_report(result, sqlite_info, sessions_info=None):
    s = result["summary"]
    tg_result = result["telegram"]
    l = result["litellm"]

    print()
    print(BOLD("═" * 60))
    print(BOLD("      OpenClaw 调用链路审计报告"))
    print(BOLD("═" * 60))
    print(f"  生成时间:  {now_local().strftime('%Y-%m-%d %H:%M:%S')} (+07:00)")
    print(f"  数据来源:  OpenClaw日志 + Litellm日志")

    # ── Health Score ──
    score = 100
    deductions = []
    if s["llm_timeouts"] > 0:
        score -= min(s["llm_timeouts"] * 15, 60)
        deductions.append(f"LLM超时 {s['llm_timeouts']}次")
    if s["litellm_upstream_timeouts"] > 0:
        score -= min(s["litellm_upstream_timeouts"] * 10, 40)
        deductions.append(f"Litellm上游超时 {s['litellm_upstream_timeouts']}次")
    if s["failovers"] > 0:
        score -= min(s["failovers"] * 5, 20)
    if s["incomplete_turns"] > 0:
        score -= min(s["incomplete_turns"] * 10, 20)
    if s["context_overflows"] > 5:
        score -= 5
    if s["connection_issues"] > 3:
        score -= 5
    score = max(score, 0)
    score_color = GREEN if score >= 80 else (YELLOW if score >= 50 else RED)
    print()
    print(f"  健康评分:  {score_color(f'{score}/100')}")
    if deductions:
        reasons = ", ".join(deductions)
        print(f"  {DIM(f'扣分原因: {reasons}')}")

    # ── 指标 ──
    print()
    print(BOLD("  📊 总体指标"))
    print(f"     ├─ Telegram 消息:        {CYAN(str(s['telegram_in']))} 条")
    print(f"     ├─ Telegram 回复:        {s['telegram_out']} 条 (错误: {RED(str(tg_result['errors'])) if tg_result['errors'] else '0'})")
    print(f"     ├─ LLM 调用错误:         {RED(str(s['llm_errors'])) if s['llm_errors'] else GREEN('0')} 次")
    print(f"     ├─ LLM 超时:             {RED(str(s['llm_timeouts'])) if s['llm_timeouts'] else GREEN('0')} 次")
    print(f"     ├─ Failover:             {YELLOW(str(s['failovers'])) if s['failovers'] else GREEN('0')} 次")
    print(f"     ├─ 上下文溢出:            {YELLOW(str(s['context_overflows'])) if s['context_overflows'] else GREEN('0')} 次")
    print(f"     ├─ 上下文压缩成功率:      {s['compaction_success']}/{s['compaction_success'] + result['context']['compactions']['incomplete']}")
    print(f"     ├─ 不完整响应:            {RED(str(s['incomplete_turns'])) if s['incomplete_turns'] else GREEN('0')} 次")
    print(f"     ├─ Telegram断连:          {YELLOW(str(s['connection_issues'])) if s['connection_issues'] else GREEN('0')} 次")
    print(f"     ├─ 配置热加载:            {s['config_reloads']} 次")
    print(f"     └─ Edit工具失败:          {YELLOW(str(s['edit_fails'])) if s['edit_fails'] else GREEN('0')} 次")

    # ── LiteLLM 指标 ──
    print()
    print(BOLD("  🔌 LiteLLM 网关状态"))
    codes_str = ", ".join(f"{c}: {n}" for c, n in sorted(l["status_codes"].items()))
    print(f"     ├─ 总请求量:              {l['total_requests']} 次")
    print(f"     ├─ 流式响应:              {l['streaming_responses']} 次")
    print(f"     ├─ 状态码分布:            {codes_str}")
    print(f"     ├─ 上游超时:              {RED(str(l['upstream_timeouts'])) if l['upstream_timeouts'] else GREEN('0')} 次")
    print(f"     └─ 上游连接错误:          {RED(str(l['upstream_errors'])) if l['upstream_errors'] else GREEN('0')} 次")

    if l["warnings"]:
        print(f"     └─ 配置警告:              {YELLOW(str(l['warnings']))} 次 (set_verbose 已弃用)")

    # ── LLM 延时 ──
    print()
    print(BOLD("  ⏱  LLM 调用延时"))
    if s.get("avg_llm_latency"):
        avg = s["avg_llm_latency"]
        p95 = s.get("p95_llm_latency", 0)
        max_l = s.get("max_llm_latency", 0)
        print(f"     ├─ 平均:    {fmt_duration(avg)}")
        print(f"     ├─ P95:     {fmt_duration(p95)}")
        print(f"     └─ 最大:    {fmt_duration(max_l)}")
    else:
        print(f"     └─ {'(无数据)' if s['llm_errors'] == 0 else '(仅错误, 无成功调用)'}")

    # ── 按小时分布 ──
    if result["time_series"]:
        print()
        print(BOLD("  🕐 消息量按小时分布"))
        max_count = max(result["time_series"].values()) or 1
        for hour, count in sorted(result["time_series"].items()):
            bar = "█" * int(count / max_count * 30)
            print(f"     {hour}  {bar} {count}")

    # ── SQLite ──
    if sqlite_info:
        print()
        print(BOLD("  🗄️  数据库 (SQLite)"))
        if "subagent_count" in sqlite_info:
            print(f"     ├─ 子Agent调用:     {sqlite_info['subagent_count']} 次")
            print(f"     ├─ 平均耗时:        {fmt_duration(sqlite_info.get('subagent_avg_dur', 0))}")
            print(f"     └─ 最大耗时:        {fmt_duration(sqlite_info.get('subagent_max_dur', 0))}")
        if "flows" in sqlite_info:
            print(f"     └─ Flow状态:        {sqlite_info['flows']}")
        if "ingress" in sqlite_info:
            print(f"     └─ Telegram入口:    {sqlite_info['ingress']}")

    # ── Session 上下文用量 ──
    if sessions_info and sessions_info.get("active"):
        print()
        print(BOLD("  💬 Session 上下文用量"))
        for sess in sessions_info["active"]:
            pct = sess.get("usagePct")
            has_tokens = sess.get("hasTokens", False)
            status = sess.get("status", "unknown")
            kind = sess["kind"]
            model = sess["model"]
            updated_display = sess.get("clientUpdatedAt", "")
            timestamp_tag = f" {DIM(chr(64) + updated_display)}" if updated_display else ""
            # 失败会话优先标记
            if sess.get("isFailed"):
                print(f"     [{kind:8}] {RED('❌ FAILED')} {'—'} {sess.get('totalTokens', '?')}/{sess['contextTokens']:,} {model}{timestamp_tag}")
            elif not has_tokens:
                print(f"     [{kind:8}] {'N/A':>5} {'—'} {sess.get('totalTokens', '?')}/{sess['contextTokens']:,} {model}{timestamp_tag}")
            else:
                pct_val = pct or 0
                bar_len = int(pct_val / 100 * 25)
                bar = "█" * bar_len
                color_f = GREEN if pct_val < 50 else (YELLOW if pct_val < 80 else RED)
                total_display = f"{sess['totalTokens']:,}"
                ctx_display = f"{sess['contextTokens']:,}"
                print(f"     [{kind:8}] {color_f(f'{pct_val:5.1f}%')} {bar} {total_display}/{ctx_display} {model}{timestamp_tag}")

    # ── 关键事件 ──
    events = result.get("raw_events", [])
    if events:
        print()
        print(BOLD("  📋 关键事件列表 (最新优先)"))
        limit = 100
        for ev in events[:limit]:
            pt = parse_ts(ev["time"])
            ts_display = pt.strftime("%H:%M:%S") if pt else (ev["time"][:16] if ev["time"] else "??")
            src = ev.get("source", "?")
            if src == "litellm":
                src_tag = DIM("[L]")
            else:
                src_tag = ""
            detail = ev.get("detail", "")[:100]
            level = ev.get("level", "")
            prefix = RED("✗") if level == "ERROR" else (YELLOW("!") if level == "WARN" else "·")
            print(f"     {prefix} {DIM(ts_display)} {src_tag} {ev['type']}")
            if detail:
                print(f"       {DIM(detail)}")

        if len(events) > limit:
            print(f"     {DIM(f'... 还有 {len(events) - limit} 条事件 (用 --since 缩小范围)')}")

    # ── 建议 ──
    print()
    print(BOLD("  💡 建议"))
    suggestions = []
    if s["llm_timeouts"] >= 3:
        suggestions.append("🔴 LLM 频繁超时 — 检查 litellm upstream 响应速度, 或降低 litellm 的 request_timeout 配置")
    if l["upstream_timeouts"] >= 5:
        suggestions.append("🔴 Litellm 上游(agnes)频繁超时 — 上游 API 响应慢, 建议检查 agnes API 状态或增大 timeout 配置")
    if s["context_overflows"] > 3:
        suggestions.append("🟡 上下文溢出频繁 — 考虑定期 /new 开始新对话, 或减少单轮 Tool 调用量")
    if s["connection_issues"] > 2:
        suggestions.append("🟡 Telegram连接不稳 — 检查网络/代理, 多数自动恢复")
    if s["edit_fails"] > 5:
        suggestions.append("🟡 Edit工具失败过多 — 长文本编辑建议用 Write 替代 Edit")
    if tg_result["errors"] > 0:
        suggestions.append("🔴 Telegram 回复失败 — 需关注 LLM 链路可用性")
    if l["warnings"] > 0:
        suggestions.append("🟡 Litellm 配置警告 — 将 set_verbose 改为 LITELLM_LOG=DEBUG 环境变量")
    if not suggestions:
        suggestions.append("✅ 系统运行正常")

    for sug in suggestions:
        print(f"     {sug}")
    print()
    print(BOLD("═" * 60))
    print()


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

    HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenClaw Audit Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, 'SF Mono', Menlo, monospace;
         background: #1a1b26; color: #c0caf5; padding: 20px; }
  .container { max-width: 1200px; margin: 0 auto; }
  h1 { color: #7aa2f7; font-size: 1.5em; margin-bottom: 20px;
       border-bottom: 1px solid #334; padding-bottom: 10px; }
  h2 { color: #89ddff; font-size: 1.1em; margin: 20px 0 10px; }
  .stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px,1fr));
           gap: 10px; margin-bottom: 20px; }
  .card { background: #24283b; border-radius: 8px; padding: 12px;
          border: 1px solid #334; }
  .card .label { font-size: 0.75em; color: #565f89; }
  .card .value { font-size: 1.5em; font-weight: bold; margin: 4px 0; }
  .card .sublabel { font-size: 0.7em; color: #565f89; }
  .card-section { font-size: 0.65em; color: #565f89; margin-top: 6px;
                   padding-top: 6px; border-top: 1px solid #334; }
  .good { color: #9ece6a; } .warn { color: #e0af68; } .bad { color: #f7768e; }
  .events { background: #24283b; border-radius: 8px; padding: 12px;
            border: 1px solid #334; max-height: 600px; overflow-y: auto; }
  .events::-webkit-scrollbar { width: 6px; }
  .events::-webkit-scrollbar-thumb { background: #334; border-radius: 3px; }
  .event { padding: 3px 0; border-bottom: 1px solid #1a1b26;
           font-size: 0.82em; display: flex; gap: 8px; align-items: baseline; }
  .event .ts { color: #565f89; white-space: nowrap; font-size: 0.9em; }
  .event .tag { padding: 1px 6px; border-radius: 3px; white-space: nowrap;
                font-size: 0.9em; }
  .tag-litellm { background: #bb9af722; color: #bb9af7; }
  .tag-error { background: #f7768e22; color: #f7768e; }
  .tag-warn { background: #e0af6822; color: #e0af68; }
  .tag-info { background: #7aa2f722; color: #7aa2f7; }
  .event .detail { color: #a9b1d6; overflow: hidden; text-overflow: ellipsis; }
  .bar-chart { margin: 10px 0; }
  .bar-row { display: flex; align-items: center; gap: 8px; margin: 2px 0; }
  .bar-label { width: 40px; text-align: right; font-size: 0.8em; color: #565f89; }
  .bar-fill { height: 18px; background: #7aa2f7; border-radius: 3px;
              min-width: 4px; transition: width 0.3s; }
  .bar-count { font-size: 0.8em; color: #565f89; }
  .controls { margin: 10px 0; display: flex; gap: 10px; align-items: center; }
  .controls select, .controls button { background: #24283b; color: #c0caf5;
    border: 1px solid #334; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
  .controls select:hover, .controls button:hover { background: #334; }
  .refresh { color: #565f89; font-size: 0.85em; margin-left: auto; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 800px) { .grid-2 { grid-template-columns: 1fr; } }
  .section-label { font-size: 0.8em; color: #565f89; margin: 10px 0 5px; }
</style>
</head>
<body>
<div class="container">
  <h1>🔍 OpenClaw + LiteLLM Audit Dashboard</h1>
  <div class="controls">
    <select id="range" onchange="load()">
      <option value="1h" {% if sel=='1h' %}selected{% endif %}>最近1小时</option>
      <option value="3h" {% if sel=='3h' %}selected{% endif %}>最近3小时</option>
      <option value="6h" {% if sel=='6h' %}selected{% endif %}>最近6小时</option>
      <option value="24h" {% if sel=='24h' %}selected{% endif %}>最近24小时</option>
      <option value="today" {% if sel=='today' %}selected{% endif %}>今天</option>
      <option value="yesterday" {% if sel=='yesterday' %}selected{% endif %}>昨天</option>
    </select>
    <button onclick="load()">🔄 刷新</button>
    <span class="refresh">自动刷新: <span id="countdown">30</span>s</span>
  </div>

  <div class="section-label">OpenClaw 网关</div>
  <div class="stats">
    <div class="card">
      <div class="label">Telegram 消息</div>
      <div class="value good">{{ data.summary.telegram_in }}</div>
      <div class="sublabel">回复 {{ data.summary.telegram_out }} 条{% if data.telegram.errors > 0 %} / {{ data.telegram.errors }} ❌{% endif %}</div>
    </div>
    <div class="card">
      <div class="label">Failover</div>
      <div class="value {% if data.failovers > 0 %}warn{% else %}good{% endif %}">{{ data.failovers }}</div>
    </div>
    <div class="card">
      <div class="label">上下文溢出</div>
      <div class="value {% if data.context.overflows > 3 %}warn{% else %}good{% endif %}">{{ data.context.overflows }}</div>
      <div class="sublabel">压缩 {{ data.summary.compaction_success }}/{{ data.summary.compaction_success + data.context.compactions.incomplete }}</div>
    </div>
    <div class="card">
      <div class="label">不完整响应</div>
      <div class="value {% if data.incomplete_turns > 0 %}bad{% else %}good{% endif %}">{{ data.incomplete_turns }}</div>
    </div>
    <div class="card">
      <div class="label">Telegram 断连</div>
      <div class="value {% if data.connection_issues > 2 %}warn{% else %}good{% endif %}">{{ data.connection_issues }}</div>
    </div>
    <div class="card">
      <div class="label">Edit 工具失败</div>
      <div class="value {% if data.tool_errors.edit > 10 %}warn{% else %}good{% endif %}">{{ data.tool_errors.edit }}</div>
    </div>
  </div>

  <div class="section-label">LiteLLM 网关</div>
  <div class="stats">
    <div class="card">
      <div class="label">总请求量</div>
      <div class="value good">{{ data.litellm.total_requests }}</div>
      <div class="sublabel">流式 {{ data.litellm.streaming_responses }} 次</div>
    </div>
    <div class="card">
      <div class="label">上游超时</div>
      <div class="value {% if data.litellm.upstream_timeouts > 0 %}bad{% else %}good{% endif %}">{{ data.litellm.upstream_timeouts }}</div>
    </div>
    <div class="card">
      <div class="label">上游连接错误</div>
      <div class="value {% if data.litellm.upstream_errors > 0 %}bad{% else %}good{% endif %}">{{ data.litellm.upstream_errors }}</div>
    </div>
    <div class="card">
      <div class="label">状态码</div>
      <div class="value" style="font-size: 1em">
        {% for code, cnt in data.litellm.status_codes.items()|sort %}
          <span>{{ code }}: {{ cnt }}</span>{% if not loop.last %} / {% endif %}
        {% endfor %}
      </div>
    </div>
  </div>

  <div class="section-label">LLM 调用延时</div>
  <div class="stats">
    <div class="card">
      <div class="label">平均延时</div>
      <div class="value" style="font-size: 1.2em">{{ data.summary.avg_llm_latency if data.summary.avg_llm_latency else 'N/A' }}</div>
    </div>
    <div class="card">
      <div class="label">P95</div>
      <div class="value" style="font-size: 1.2em">{{ data.summary.p95_llm_latency if data.summary.p95_llm_latency else 'N/A' }}</div>
    </div>
    <div class="card">
      <div class="label">最大</div>
      <div class="value" style="font-size: 1.2em">{{ data.summary.max_llm_latency if data.summary.max_llm_latency else 'N/A' }}</div>
    </div>
    <div class="card">
      <div class="label">LLM 错误/超时</div>
      <div class="value {% if data.summary.llm_errors > 0 or data.summary.llm_timeouts > 0 %}bad{% else %}good{% endif %}">
        {{ data.summary.llm_errors }} / {{ data.summary.llm_timeouts }}
      </div>
    </div>
  </div>

  <div class="section-label">Session 上下文</div>
  <div class="stats">
  {% for sess in data.sessions.active %}
    {% set has_tok = sess.get('hasTokens', False) %}
    {% set pct = sess.usagePct %}
    {% set sess_failed = sess.get('isFailed', False) %}
    {% if sess_failed %}
      {% set cls = 'bad' %}
    {% elif has_tok %}
      {% if pct < 50 %}{% set cls = 'good' %}{% elif pct < 80 %}{% set cls = 'warn' %}{% else %}{% set cls = 'bad' %}{% endif %}
    {% else %}
      {% set cls = '' %}
    {% endif %}
    <div class="card">
      <div class="label">{{ sess.kind }} ({{ sess.model }}) <span style="color:#565f89;font-weight:normal">@{{ sess.clientUpdatedAt if sess.clientUpdatedAt else '??' }}</span></div>
      <div class="value {{ cls }}">{% if sess_failed %}FAILED{% elif has_tok %}{{ '%.0f'|format(pct) }}%{% else %}N/A{% endif %}</div>
      <div class="sublabel">{% if sess_failed %}❌ 子会话已失败{% elif has_tok %}{{ '{:,}'.format(sess.totalTokens) }}/{{ '{:,}'.format(sess.contextTokens) }}{% else %}N/A/{% endif %}</div>
    </div>
  {% endfor %}
  {% if data.sessions.error %}
    <div class="card">
      <div class="label">Session 查询失败</div>
      <div class="value bad" style="font-size:1em">{{ data.sessions.error }}</div>
    </div>
  {% elif not data.sessions.active %}
    <div class="card"><div class="value" style="font-size:1em">暂无会话数据</div></div>
  {% endif %}
  </div>

  <div class="grid-2">
    <div>
      <h2>🕐 消息分布</h2>
      <div class="bar-chart">
      {% for hour, count in data.time_series|dictsort %}
        {% set max_c = data.time_series.values()|max %}
        {% set pct = (count / max_c * 100)|round if max_c > 0 else 0 %}
        <div class="bar-row">
          <span class="bar-label">{{ hour }}</span>
          <div class="bar-fill" style="width: {{ pct }}%"></div>
          <span class="bar-count">{{ count }}</span>
        </div>
      {% endfor %}
      </div>
    </div>
    <div>
      <h2>💡 建议</h2>
      <div class="events">
      {% for s in suggestions %}
        <div class="event"><span>{{ s }}</span></div>
      {% endfor %}
      </div>
    </div>
  </div>

  <h2>📋 关键事件 (最新优先)</h2>
  <div class="events" id="event-list">
  {% for ev in data.raw_events[:200] %}
    {% set tag_class = 'tag-info' %}
    {% if ev.source == 'litellm' %}{% set tag_class = 'tag-litellm' %}{% endif %}
    {% if ev.level == 'ERROR' %}{% set tag_class = 'tag-error' %}
    {% elif ev.level == 'WARN' %}{% set tag_class = 'tag-warn' %}{% endif %}
    <div class="event">
      <span class="ts">{{ ev.time[:16] if ev.time else '??' }}</span>
      <span class="tag {{ tag_class }}">{{ ev.type }}</span>
      <span class="detail">{{ ev.detail[:150] }}</span>
    </div>
  {% endfor %}
  {% if not data.raw_events %}
    <div class="event"><span class="good">✅ 无异常事件</span></div>
  {% elif data.raw_events|length > 200 %}
    <div class="event"><span class="warn">… 还有 {{ data.raw_events|length - 200 }} 条 (用 --since 缩小范围)</span></div>
  {% endif %}
  </div>

  <div style="margin-top: 10px; color: #565f89; font-size: 0.75em;">
    OpenClaw+LiteLLM Audit v1.1 — 数据来源: openclaw-*.log + litellm.err.log
  </div>
</div>
<script>
let timer = 30;
function countdown() {
  timer--;
  document.getElementById('countdown').textContent = timer;
  if (timer <= 0) { timer = 30; load(); }
}
setInterval(countdown, 1000);
async function load() {
  const range = document.getElementById('range').value;
  window.location.href = '/?since=' + range;
}
</script>
</body>
</html>"""

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
        s = data["summary"]
        l = data["litellm"]

        suggestions = []
        if s["llm_timeouts"] >= 3:
            suggestions.append("🔴 LLM 频繁超时 — 检查 litellm upstream 响应速度")
        if l["upstream_timeouts"] >= 5:
            suggestions.append("🔴 Litellm 上游超时(agnes) — 检查 API 状态或增大 timeout")
        if data["context"]["overflows"] > 3:
            suggestions.append("🟡 上下文溢出频繁 — 考虑 /new 开始新对话")
        if data["connection_issues"] > 2:
            suggestions.append("🟡 Telegram连接不稳")
        if data["tool_errors"]["edit"] > 5:
            suggestions.append("🟡 Edit失败过多")
        if data["telegram"]["errors"] > 0:
            suggestions.append("🔴 Telegram 回复失败")
        if l["warnings"] > 0:
            suggestions.append("🟡 Litellm 配置警告 — set_verbose → LITELLM_LOG=DEBUG")
        if not suggestions:
            suggestions.append("✅ 系统运行正常")

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
