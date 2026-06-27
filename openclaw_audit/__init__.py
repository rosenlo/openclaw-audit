"""openclaw_audit — audit tool for the Telegram → OpenClaw → LiteLLM chain.

The package holds all parsing, classification, analysis, querying and
rendering logic. The top-level ``openclaw-audit.py`` script is a thin
CLI/web/watch wrapper that imports from here.
"""

from .config import LOCAL_TZ, now_local
from .util import (
    BOLD, CYAN, DIM, GREEN, RED, YELLOW,
    _extract_fields, _fmt_ts, _parse_int_field, _session_id_from_key, _truncate,
    color, fmt_duration, parse_ts,
)
from .classify import classify_entry, classify_litellm_entry
from .parsing import (
    parse_litellm_err_log, parse_litellm_out_log,
    parse_openclaw_log, parse_openclaw_logs_since,
)
from .analyze import analyze
from .insights import build_root_cause_summary, build_suggestions
from .queries import query_sessions, query_sqlite
from .render import HTML_TEMPLATE, print_report

__all__ = [
    # config
    "LOCAL_TZ", "now_local",
    # util
    "BOLD", "CYAN", "DIM", "GREEN", "RED", "YELLOW",
    "_extract_fields", "_fmt_ts", "_parse_int_field", "_session_id_from_key", "_truncate",
    "color", "fmt_duration", "parse_ts",
    # classify
    "classify_entry", "classify_litellm_entry",
    # parsing
    "parse_litellm_err_log", "parse_litellm_out_log",
    "parse_openclaw_log", "parse_openclaw_logs_since",
    # analyze
    "analyze",
    # insights
    "build_root_cause_summary", "build_suggestions",
    # queries
    "query_sessions", "query_sqlite",
    # render
    "HTML_TEMPLATE", "print_report",
]
