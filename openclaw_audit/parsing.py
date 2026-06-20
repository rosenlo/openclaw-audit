"""Log file parsing for OpenClaw and LiteLLM logs."""

import glob
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta as _td

from .config import LOG_DIR, LITELLM_ERR_LOG, LITELLM_OUT_LOG, now_local
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
                # logLevelName lives under _meta in real OpenClaw logs (the
                # top-level field is absent), so look there too — otherwise
                # every line's level comes back empty and all WARN/ERROR
                # events get mis-bucketed as "other" and silently dropped.
                level = d.get("logLevelName", "")
                if not level:
                    meta = d.get("_meta")
                    if isinstance(meta, dict):
                        level = meta.get("logLevelName", "")
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
# LiteLLM 写 err.log 有两种格式，由它进程内的 _logging.py 决定：
#   1) 默认文本：`HH:MM:SS - LiteLLM Router:ERROR: file.py:97 - ...`
#      datefmt="%H:%M:%S" 是写死的，config.yaml 没暴露，所以没有日期。
#   2) JSON（设 JSON_LOGS=true 启用 JsonFormatter）：每行一个 JSON 对象，
#      timestamp = datetime.fromtimestamp(record.created).isoformat()，
#      带完整日期年份，是根治“只有时分秒需要推断”的来源。
# 优先按 JSON 解析；失败再回退文本格式。文本格式仍保留跨午夜推断，但
# 加阈值，避免同日内日志乱序被误判成跨天。
ansi_pat = re.compile(r"\033\[[0-9;]*m")
text_line_pat = re.compile(
    r"^(\d{2}:\d{2}:\d{2})\s*-\s*LiteLLM\s+(\S+):(\S+):\s+(.*)$"
)
# 同日内日志乱序（多 worker / 缓冲刷新 / 同秒多条）导致的时间倒退幅度一般
# 在几分钟以内；真正的跨午夜倒退是从深夜回到凌晨，幅度巨大（>12h）。
# 用 12 小时作为阈值：倒退超过 12h 才认为是跨了午夜，否则视为同日乱序，
# 保持当天日期不再 +1。这样 22:25 -> 22:22 这种不会被标成明天。
_MIDNIGHT_BACKSTEP_THRESHOLD_SECONDS = 12 * 3600


def _backstep_seconds(prev_hms, cur_hms):
    """Seconds from cur_hms back to prev_hms, assuming same day. cur < prev."""
    base = datetime(2000, 1, 1)
    a = base.replace(hour=prev_hms.hour, minute=prev_hms.minute, second=prev_hms.second)
    b = base.replace(hour=cur_hms.hour, minute=cur_hms.minute, second=cur_hms.second)
    return (a - b).total_seconds()


def _litellm_json_entry(obj, since_ts):
    """Build a (source, full_ts, level, msg) tuple from a JSON log line, or
    None when the line isn't a usable LiteLLM JSON record."""
    if not isinstance(obj, dict):
        return None
    ts_str = obj.get("timestamp")
    msg = obj.get("message")
    if not ts_str or not msg:
        return None
    parsed = parse_ts(ts_str)
    if parsed is None:
        return None
    if since_ts is not None and parsed.timestamp() < since_ts:
        return None
    level = obj.get("level", "INFO")
    component = obj.get("component", "LiteLLM")
    logger = obj.get("logger")  # "filename:lineno"
    detail = f"[{component}] {logger} - {msg}" if logger else f"[{component}] {msg}"
    full_ts = parsed.strftime("%Y-%m-%dT%H:%M:%S") + "+07:00"
    return ("litellm", full_ts, level, detail)


def parse_litellm_err_log(since=None):
    """Parse litellm.err.log. Supports JSON logs (JSON_LOGS=true, preferred)
    and the legacy `HH:MM:SS - LiteLLM ...:` text format."""
    entries = []
    if not os.path.exists(LITELLM_ERR_LOG):
        return entries

    since_ts = None
    if since:
        since_ts = since.timestamp()

    # 文本格式的日期推断游标：只有 HH:MM:SS 时按行序走，跨午夜才 +1 天。
    cur_date = now_local().date()
    prev_hms = None

    try:
        with open(LITELLM_ERR_LOG) as f:
            for line in f:
                clean = ansi_pat.sub("", line).strip()
                if not clean:
                    continue

                # 优先 JSON（JSON_LOGS=true 输出）。
                if clean.startswith("{"):
                    try:
                        obj = json.loads(clean)
                    except json.JSONDecodeError:
                        obj = None
                    if obj is not None:
                        ent = _litellm_json_entry(obj, since_ts)
                        if ent is not None:
                            entries.append(ent)
                        continue

                # 回退到文本格式。
                m = text_line_pat.match(clean)
                if m:
                    ts_str = m.group(1)
                    component = m.group(2)
                    level = m.group(3)
                    msg = m.group(4)

                    parsed = parse_ts(ts_str)
                    if parsed and since_ts is not None and parsed.timestamp() < since_ts:
                        continue

                    cur_hms = parsed.time() if parsed else None
                    if (
                        prev_hms is not None
                        and cur_hms is not None
                        and cur_hms < prev_hms
                        and _backstep_seconds(prev_hms, cur_hms)
                        >= _MIDNIGHT_BACKSTEP_THRESHOLD_SECONDS
                    ):
                        # 倒退幅度足够大才算真的跨了午夜；小幅倒退是同日
                        # 乱序，保持当天日期，避免把 22:22 标成明天。
                        cur_date = cur_date + _td(days=1)
                    prev_hms = cur_hms

                    full_ts = f"{cur_date}T{ts_str}+07:00"
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
