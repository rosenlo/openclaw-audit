"""Shared helpers: timestamp parsing, formatting, field extraction, colors."""

import re
import sys
from datetime import datetime, timezone

from .config import LOCAL_TZ, TODAY


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


def _session_id_from_key(key):
    """Extract the trailing session id from a colon-delimited session key."""
    if not key:
        return ""
    return key.split(":")[-1]


def _extract_fields(msg, wanted):
    """Extract key=value fields from a log message."""
    wanted = set(wanted)
    found = {}
    for m in re.finditer(r"(\w+)=([^\s]+)", msg):
        key = m.group(1)
        if key in wanted:
            found[key] = m.group(2)
    return found


def _parse_int_field(part):
    """Parse `key=value` into int, or None when value is non-numeric
    (e.g. OpenClaw emits `messages=NaN` at the precheck stage)."""
    try:
        return int(part.split("=", 1)[1])
    except (ValueError, IndexError):
        return None


def fmt_duration(sec):
    if sec is None:
        return "N/A"
    if sec < 1:
        return f"{sec*1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(sec), 60)
    return f"{m}m{s}s"


# ─── CLI 颜色 ───────────────────────────────────────────────────────
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
