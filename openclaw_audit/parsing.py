"""Log file parsing for OpenClaw and LiteLLM logs."""

import glob
import json
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime

from .config import (
    LITELLM_ERR_LOG,
    LITELLM_LOG_KEEP,
    LITELLM_LOG_MAX_SIZE_BYTES,
    LITELLM_OUT_LOG,
    LOG_DIR,
    LOCAL_TZ,
)
from .util import parse_ts, tz_offset_str


# ─── OpenClaw 日志解析 ─────────────────────────────────────────────
# Per-file parse cache: filepath -> (mtime_ns, size, entries).
# In watch mode the same log files get re-parsed every interval; if mtime
# and size are unchanged we skip re-reading and return the cached entries
# (shallow-copied so callers can extend/sort freely). Cache is process-local
# and only saves work within a single watch run — never persisted.
_FILE_CACHE: dict = {}


def _cached_parse(filepath, parser_fn):
    try:
        st = os.stat(filepath)
    except OSError:
        return []
    cached = _FILE_CACHE.get(filepath)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return list(cached[2])
    entries = parser_fn()
    _FILE_CACHE[filepath] = (st.st_mtime_ns, st.st_size, list(entries))
    return entries


def parse_openclaw_log(filepath):
    """Parse an OpenClaw JSON log file (mtime-cached)."""
    return _cached_parse(filepath, lambda: _parse_openclaw_log_raw(filepath))


def _parse_openclaw_log_raw(filepath):
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
# LiteLLM err.log 在 JSON_LOGS=true 时每行一个 JSON 对象,timestamp 带完整日期。
# 老的 `HH:MM:SS - LiteLLM ...:` 文本格式没有日期,先前用 cur_date=今天 fallback
# 会让历史事件漂移到未来 (06-15 的事件被标成今天,跨午夜逻辑又把它推到明天)。
# litellm 06-20 已切到 JSON_LOGS,文本行都是死历史,不再产生,直接跳过。
# 若你的 err.log 仍有文本行,在 LiteLLM 进程设 JSON_LOGS=true (注意不是
# LITELLM_LOG=JSON,后者会被当成日志级别解析报错)。


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
    full_ts = parsed.strftime("%Y-%m-%dT%H:%M:%S") + tz_offset_str(LOCAL_TZ)
    return ("litellm", full_ts, level, detail)


def parse_litellm_err_log(since=None):
    """Parse litellm.err.log (JSON_LOGS=true format, mtime-cached).

    Legacy ``HH:MM:SS - LiteLLM ...:`` text-format lines are skipped: they
    carry no date and were previously mis-stamped as 'today', drifting
    historical events into the future. Enable ``JSON_LOGS=true`` on the
    LiteLLM process if your err.log still contains text-format lines.
    """
    return _cached_parse(
        LITELLM_ERR_LOG, lambda: _parse_litellm_err_log_raw(since)
    )


def _parse_litellm_err_log_raw(since=None):
    entries = []
    if not os.path.exists(LITELLM_ERR_LOG):
        return entries

    since_ts = None
    if since:
        since_ts = since.timestamp()

    try:
        with open(LITELLM_ERR_LOG) as f:
            for line in f:
                clean = line.strip()
                if not clean or not clean.startswith("{"):
                    continue
                try:
                    obj = json.loads(clean)
                except json.JSONDecodeError:
                    continue
                ent = _litellm_json_entry(obj, since_ts)
                if ent is not None:
                    entries.append(ent)
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


# ─── LiteLLM 日志轮转 ──────────────────────────────────────────────
# litellm 由 launchd 启动,stdout/stderr 被 launchd 重定向到 err.log/out.log。
# launchd 不会轮转这些文件,所以 audit 在启动时检查大小并 copytruncate 轮转:
#   1) 当前文件复制为 .1 (老的 .1 -> .2, ..., .keep 被删除)
#   2) 原地清空当前文件 (litellm 的 fd 仍指向同一 inode,继续写入新内容)
# copytruncate 在复制和清空之间有毫秒级窗口可能丢日志,但 litellm 不能 reopen
# signal,这是不重启进程轮转的唯一方式。

def rotate_litellm_logs(keep=None):
    """Force-rotate litellm.err.log and litellm.out.log now.

    Uses copytruncate so the LiteLLM process (whose stdout/stderr fd is
    held open by launchd) keeps writing to the same inode without a
    restart. Returns ``{path: {"rotated": bool, "reason": str}}``.
    """
    if keep is None:
        keep = LITELLM_LOG_KEEP
    return {
        LITELLM_ERR_LOG: _rotate_one(LITELLM_ERR_LOG, keep),
        LITELLM_OUT_LOG: _rotate_one(LITELLM_OUT_LOG, keep),
    }


def maybe_rotate_litellm_logs(max_size_bytes=None, keep=None):
    """Rotate litellm logs if any file exceeds ``max_size_bytes``.

    Returns ``{path: {"rotated": bool, "reason": str}}``; entries with
    ``rotated=False`` indicate no action (file missing or too small).
    """
    if max_size_bytes is None:
        max_size_bytes = LITELLM_LOG_MAX_SIZE_BYTES
    if keep is None:
        keep = LITELLM_LOG_KEEP
    results = {}
    for path in (LITELLM_ERR_LOG, LITELLM_OUT_LOG):
        try:
            st = os.stat(path)
        except OSError:
            results[path] = {"rotated": False, "reason": "missing"}
            continue
        if st.st_size < max_size_bytes:
            results[path] = {
                "rotated": False,
                "reason": f"size={st.st_size} < {max_size_bytes}",
            }
            continue
        results[path] = _rotate_one(path, keep)
    return results


def _rotate_one(path, keep):
    """copytruncate-rotate a single log file in place.

    Shifts existing ``.{i}`` files (highest dropped), copies current to
    ``.1``, then truncates the current file so the writer's open fd stays
    valid. Not concurrency-guarded; callers that need cross-process safety
    should hold an external lock.
    """
    if not os.path.exists(path):
        return {"rotated": False, "reason": "missing"}
    try:
        # Shift .{keep-1} -> .{keep} (dropped), ..., .1 -> .2.
        # Iterate high to low so we don't clobber an existing .{i+1}.
        for i in range(keep, 0, -1):
            src = f"{path}.{i}"
            if not os.path.exists(src):
                continue
            if i >= keep:
                os.unlink(src)
            else:
                os.rename(src, f"{path}.{i + 1}")
        # Copy current -> .1, then truncate in place.
        shutil.copy2(path, f"{path}.1")
        with open(path, "w"):
            pass
    except OSError as e:
        return {"rotated": False, "reason": f"{type(e).__name__}: {e}"}
    return {"rotated": True, "reason": f"copytruncated (kept {keep})"}
