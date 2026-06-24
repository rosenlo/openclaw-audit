"""Path and timezone configuration for openclaw-audit.

All paths can be overridden via environment variables so the public
repository does not hardcode any specific user paths. When an env var is
absent a generic fallback is used.
"""

import os
import re
from datetime import datetime, timedelta, timezone

# ─── 路径配置 ───────────────────────────────────────────────────────
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
# 优先级:
#   1) OPENCLAW_AUDIT_TZ 显式覆盖 (+07:00 / -05:00 / +05:30 / +0700 / +07 / UTC)
#   2) 自动从系统本地时区探测 (datetime.now().astimezone().tzinfo)
# 这样默认就不写死 +07:00, 在不同机器跑都跟系统时区走; 想固定覆盖再设 env。
_AUDIT_TZ_STR = os.environ.get("OPENCLAW_AUDIT_TZ", "").strip()


def parse_tz_str(s):
    """Parse a tz string into a ``timezone`` object.

    Accepts ``UTC`` (case-insensitive), ``+HH:MM``, ``-HH:MM``, ``+HHMM``,
    or ``+HH`` (including half-hour offsets like ``+05:30``). Raises
    ``ValueError`` on a malformed input so callers can surface a clear error
    instead of silently falling back.
    """
    if s.strip().upper() == "UTC":
        return timezone.utc
    m = re.match(r"^([+-]?)(\d{1,2}):?(\d{2})?$", s.strip())
    if not m:
        raise ValueError(
            f"Invalid OPENCLAW_AUDIT_TZ: {s!r} "
            "(expected +HH:MM, -HH:MM, +HHMM, +HH, or UTC)"
        )
    sign = -1 if m.group(1) == "-" else 1
    hours = int(m.group(2))
    minutes = int(m.group(3) or 0)
    return timezone(timedelta(minutes=sign * (hours * 60 + minutes)))


def _detect_system_tz():
    """Detect the system local timezone via the stdlib.

    ``datetime.now().astimezone()`` with no tzinfo argument attaches the
    system local timezone (reads /etc/localtime on POSIX, the OS tz on
    Windows). Returns a ``timezone`` (fixed offset) object. Falls back to
    UTC only if detection returns a naive datetime (shouldn't happen on
    any supported platform, but guards against containers with no tzdata).
    """
    try:
        dt = datetime.now().astimezone()
        if dt.tzinfo is not None:
            # Re-construct as a fixed-offset timezone so utcoffset() is
            # stable and tz_offset_str renders cleanly (some platforms
            # return a DstTzInfo whose utcoffset() depends on the datetime).
            return timezone(dt.utcoffset())
    except (OverflowError, OSError):
        pass
    return timezone.utc


if _AUDIT_TZ_STR:
    LOCAL_TZ = parse_tz_str(_AUDIT_TZ_STR)
else:
    LOCAL_TZ = _detect_system_tz()


def now_local():
    return datetime.now(LOCAL_TZ)


TODAY = now_local().strftime("%Y-%m-%d")
