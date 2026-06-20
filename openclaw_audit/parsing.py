"""Log file parsing for OpenClaw and LiteLLM logs."""

import glob
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

from .config import LOG_DIR, LITELLM_ERR_LOG, LITELLM_OUT_LOG, TODAY
from .util import parse_ts


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
