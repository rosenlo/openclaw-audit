"""Shared helpers: timestamp parsing, formatting, field extraction, colors."""

import re
import sys
from datetime import datetime, timedelta, timezone

from .config import LOCAL_TZ, TODAY, now_local


def tz_offset_str(tz):
    """Render a tzinfo as ±HH:MM (UTC -> +00:00).

    Used wherever we re-stamp a LiteLLM timestamp string with the audit's
    local tz so the rendered suffix matches ``LOCAL_TZ`` instead of a
    hardcoded ``+07:00`` that breaks when ``OPENCLAW_AUDIT_TZ`` is overridden.
    """
    if tz is None:
        return ""
    offset = tz.utcoffset(None)
    if offset is None:
        return ""
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    abs_total = abs(total)
    h, rem = divmod(abs_total, 3600)
    m = rem // 60
    return f"{sign}{h:02d}:{m:02d}"


def parse_since_arg(since_str):
    """Parse the ``--since`` CLI flag.

    Accepts ``Nh`` (hours), ``Nd`` (days), ``today``, ``yesterday``, or a
    ``YYYY-MM-DD`` date. Returns ``(since_datetime, label)`` where ``since``
    is tz-aware in ``LOCAL_TZ`` and ``label`` is the human string the CLI
    prints. Raises ``ValueError`` on a bad format so the caller can surface
    ``Invalid --since``. Empty/None falls back to "最近 1 小时" so callers
    don't have to special-case the default.
    """
    if not since_str:
        since = now_local() - timedelta(hours=1)
        return since, "最近 1 小时"
    s = since_str.lower()
    if s == "today":
        return now_local().replace(hour=0, minute=0, second=0, microsecond=0), "今天"
    if s == "yesterday":
        return (
            now_local().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1),
            "昨天",
        )
    if since_str.endswith("h"):
        hours = int(since_str[:-1])
        return now_local() - timedelta(hours=hours), f"最近 {hours} 小时"
    if since_str.endswith("d"):
        days = int(since_str[:-1])
        return now_local() - timedelta(days=days), f"最近 {days} 天"
    dt = datetime.strptime(since_str, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    return dt, dt.strftime("%Y-%m-%d")


def parse_ts(ts_str):
    """Parse OpenClaw ISO timestamp or litellm HH:MM:SS timestamp.

    Accepts ``±HH:MM`` offsets (and ``Z``) on ISO input. The original tz
    suffix is stripped and replaced with ``LOCAL_TZ`` because OpenClaw/litellm
    logs are produced on the same machine the audit runs on, so trusting the
    wall clock + local tz matches reality. Returns ``None`` on parse failure.
    """
    if not ts_str:
        return None
    try:
        # OpenClaw / litellm ISO: 2026-06-18T07:43:06.757+07:00, ...Z, or
        # no tz at all. Strip any trailing offset and re-stamp with LOCAL_TZ.
        if "T" in ts_str or " " in ts_str:
            ts_clean = re.sub(r"(Z|[+-]\d{2}:\d{2})$", "", ts_str)
            fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in ts_clean else "%Y-%m-%dT%H:%M:%S"
            dt = datetime.strptime(ts_clean, fmt)
            return dt.replace(tzinfo=LOCAL_TZ)
        # litellm format: HH:MM:SS
        elif re.match(r"^\d{2}:\d{2}:\d{2}", ts_str):
            today_str = TODAY
            dt = datetime.strptime(f"{today_str} {ts_str}", "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=LOCAL_TZ)
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
    """Extract key=value fields from a log message.

    Handles two value shapes:
    - ``key=value`` (no whitespace) — the common OpenClaw log shape
    - ``key="value with spaces"`` or ``key='value'`` — quoted values, used
      by announce-delivery error strings like
      ``deliveryError="completion agent did not use the message tool"``.
    """
    wanted = set(wanted)
    found = {}
    # Quoted values: key="..." or key='...' (single capture, non-greedy).
    for m in re.finditer(r'(\w+)=("([^"]*)"|\'([^\']*)\'|(\S+))', msg):
        key = m.group(1)
        if key not in wanted:
            continue
        # Prefer the quoted inner group when present, else the bare value.
        val = m.group(3) if m.group(3) is not None else (
            m.group(4) if m.group(4) is not None else m.group(5)
        )
        found[key] = val
    return found


def _parse_int_field(part):
    """Parse `key=value` into int, or None when value is non-numeric
    (e.g. OpenClaw emits `messages=NaN` at the precheck stage)."""
    try:
        return int(part.split("=", 1)[1])
    except (ValueError, IndexError):
        return None


def _truncate(s, n=300, marker="…"):
    """Truncate string to n chars; append marker when truncated.

    Unlike a bare `s[:n]`, this surfaces the truncation so a reader does
    not mistake a clipped path/error message for the full string. Returns
    the input unchanged when it already fits.
    """
    if s is None:
        return s
    if len(s) <= n:
        return s
    # Leave room for the marker so the visible width is exactly n.
    return s[: n - len(marker)] + marker


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
