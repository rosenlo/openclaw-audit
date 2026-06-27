import pytest
import openclaw_audit
from openclaw_audit import (
    analyze,
    build_root_cause_summary,
    build_suggestions,
    classify_entry,
)


def test_classify_entry_detects_stalled_session():
    msg = (
        "stalled session: sessionId=bbb0cc71-f793-4856-bedc-0e64fe71236b "
        "sessionKey=agent:main:telegram:direct:670530854 state=processing "
        "age=122s queueDepth=1 reason=active_work_without_progress "
        "classification=stalled_agent_run activeWorkKind=model_call "
        "lastProgress=model_call:started lastProgressAge=122s recovery=none"
    )

    cat = classify_entry(msg, "WARN")

    assert cat["type"] == "stalled_session"
    assert cat["reason"] == "active_work_without_progress"
    assert cat["classification"] == "stalled_agent_run"
    assert cat["state"] == "processing"
    assert cat["lastProgressAge"] == "122s"
    # model_call stalls carry no tool name — `tool` must NOT be set so
    # the display falls back to showing lastProgress verbatim.
    assert "tool" not in cat


def test_classify_entry_stalled_tool_call_extracts_tool_name():
    """When a session stalls on a tool_call, OpenClaw emits lastProgress
    as 'tool_call:<name>:<state>' (or 'tool_call:<name>'). The classifier
    must surface the tool name as `tool` so the audit detail points at
    the offending tool without the operator reading the raw progress
    string."""
    msg = (
        "stalled session: sessionId=abc state=processing age=1704s "
        "queueDepth=0 reason=blocked_tool_call classification=blocked_tool_call "
        "activeWorkKind=tool_call lastProgress=tool_call:Bash:started "
        "lastProgressAge=195s"
    )

    cat = classify_entry(msg, "ERROR")

    assert cat["type"] == "stalled_session"
    assert cat["reason"] == "blocked_tool_call"
    assert cat["activeWorkKind"] == "tool_call"
    assert cat["tool"] == "Bash"
    assert cat["lastProgress"] == "tool_call:Bash:started"


def test_classify_entry_stalled_tool_call_no_state_suffix():
    """lastProgress may be 'tool_call:<name>' with no trailing :<state>.
    The tool name must still be extracted."""
    msg = (
        "stalled session: state=processing reason=blocked_tool_call "
        "activeWorkKind=tool_call lastProgress=tool_call:Read"
    )
    cat = classify_entry(msg, "ERROR")
    assert cat["tool"] == "Read"


def test_classify_entry_stalled_tool_call_bare_state_no_tool():
    """If OpenClaw emits 'tool_call:started' (bare state, no tool name),
    we must NOT mislabel 'started' as the tool name. `tool` stays unset
    and the display falls back to showing lastProgress verbatim."""
    msg = (
        "stalled session: state=processing reason=blocked_tool_call "
        "activeWorkKind=tool_call lastProgress=tool_call:started"
    )
    cat = classify_entry(msg, "ERROR")
    assert "tool" not in cat
    assert cat["lastProgress"] == "tool_call:started"


def test_analyze_stalled_tool_call_detail_shows_tool_name():
    """The audit detail for a blocked_tool_call stall must surface
    `tool=<name>` so triage can immediately see which tool hung, instead
    of having to read lastProgress raw."""
    from openclaw_audit import analyze
    msg = (
        "stalled session: sessionId=abc "
        "sessionKey=agent:main:telegram:direct:670530854 state=processing "
        "age=1704s queueDepth=0 reason=blocked_tool_call "
        "classification=blocked_tool_call activeWorkKind=tool_call "
        "lastProgress=tool_call:Bash:started lastProgressAge=195s"
    )
    result = analyze([("openclaw", "2026-06-25T18:02:08+07:00", "ERROR", msg)])
    ev = result["raw_events"][0]
    assert "tool=Bash" in ev["detail"]
    # lastProgress should NOT also be shown when tool= is present (avoids
    # repeating the same info twice in the detail line).
    assert "lastProgress=" not in ev["detail"]
    assert "reason=blocked_tool_call" in ev["detail"]
    assert "activeWorkKind=tool_call" in ev["detail"]
    assert "sessionKey=agent:main:telegram:direct:670530854" in ev["detail"]


def test_analyze_stalled_model_call_detail_falls_back_to_lastProgress():
    """For a model_call stall (no tool name to extract), the detail must
    show lastProgress verbatim so the operator still sees the runner
    state, instead of an empty gap where tool= would have been."""
    from openclaw_audit import analyze
    msg = (
        "stalled session: sessionId=bbb state=processing age=122s "
        "queueDepth=1 reason=active_work_without_progress "
        "classification=stalled_agent_run activeWorkKind=model_call "
        "lastProgress=model_call:started lastProgressAge=122s recovery=none"
    )
    result = analyze([("openclaw", "2026-06-20T16:34:56+07:00", "WARN", msg)])
    ev = result["raw_events"][0]
    assert "lastProgress=model_call:started" in ev["detail"]
    assert "tool=" not in ev["detail"]
    assert "recovery=none" in ev["detail"]


def test_classify_entry_marks_llm_abort():
    msg = "model-fetch error provider=litellm elapsedMs=17600 AbortError: request aborted"

    cat = classify_entry(msg, "ERROR")

    assert cat["type"] == "llm_error"
    assert cat["reason"] == "abort"
    assert cat["elapsed_ms"] == 17600


def test_analyze_separates_stall_from_telegram_failure():
    entries = [
        (
            "openclaw",
            "2026-06-20T16:34:56.000+07:00",
            "WARN",
            "stalled session: sessionId=bbb0cc71-f793-4856-bedc-0e64fe71236b "
            "sessionKey=agent:main:telegram:direct:670530854 state=processing "
            "age=122s queueDepth=1 reason=active_work_without_progress "
            "classification=stalled_agent_run activeWorkKind=model_call "
            "lastProgress=model_call:started lastProgressAge=122s recovery=none",
        ),
        (
            "openclaw",
            "2026-06-20T16:35:00.000+07:00",
            "ERROR",
            "model-fetch error provider=litellm elapsedMs=17600 AbortError: request aborted",
        ),
    ]

    result = analyze(entries)
    suggestions = build_suggestions(result, {"errors": 0})

    assert result["summary"]["session_stalls"] == 1
    assert result["summary"]["llm_aborts"] == 1
    assert any("任务卡住但未必断连" in item for item in suggestions)
    assert any("LLM 请求被 abort" in item for item in suggestions)


def test_build_root_cause_summary_prioritizes_stall():
    result = {
        "summary": {
            "session_stalls": 2,
            "llm_aborts": 1,
            "llm_timeouts": 0,
            "litellm_upstream_timeouts": 0,
            "context_overflows": 0,
            "connection_issues": 0,
        },
        "litellm": {
            "proxy_exceptions": 0,
            "upstream_timeouts": 0,
            "warnings": 0,
        },
    }

    summary = build_root_cause_summary(result)

    assert "执行层卡住" in summary


def test_package_exports_public_api():
    """The thin CLI wrapper relies on these names being importable from the
    top-level package. Lock the public surface so a future move doesn't
    silently break `openclaw-audit.py`."""
    for name in [
        "analyze", "classify_entry", "classify_litellm_entry",
        "build_suggestions", "build_root_cause_summary",
        "parse_openclaw_logs_since", "parse_litellm_err_log",
        "maybe_rotate_litellm_logs", "rotate_litellm_logs",
        "query_sessions", "query_sqlite", "print_report",
        "HTML_TEMPLATE", "now_local",
    ]:
        assert hasattr(openclaw_audit, name), f"openclaw_audit missing {name}"


# ─── litellm auth classification + ordering + truncation ──────────────
AUTH_MSG = (
    "[Proxy] auth_exception_handler.py:97 - "
    "litellm.proxy.proxy_server.user_api_key_auth(): Exception occured - "
    "No api key passed"
)


def test_classify_litellm_auth_error_beats_proxy_exception():
    """The auth message contains 'Exception occured', so it would normally
    bucket as proxy_exception. Auth must win so triage points at credentials,
    not a runtime proxy fault."""
    from openclaw_audit import classify_litellm_entry

    cat = classify_litellm_entry(AUTH_MSG)
    assert cat["type"] == "auth_error"


def test_classify_litellm_generic_exception_still_proxy():
    """A non-auth 'Exception occured' line must still classify as
    proxy_exception — the auth branch must not over-match."""
    from openclaw_audit import classify_litellm_entry

    cat = classify_litellm_entry("[Router] Exception occured - something else")
    assert cat["type"] == "proxy_exception"


def test_analyze_counts_auth_and_label():
    from openclaw_audit import analyze

    entries = [
        ("litellm", "2026-06-20T22:25:03+07:00", "ERROR", AUTH_MSG),
    ]
    result = analyze(entries)

    assert result["litellm"]["auth_errors"] == 1
    assert result["litellm"]["proxy_exceptions"] == 0
    assert result["summary"]["litellm_auth_errors"] == 1
    ev = result["raw_events"][0]
    assert ev["type"] == "🔑 Litellm鉴权失败"
    assert ev["level"] == "ERROR"


def test_analyze_detail_not_truncated_short():
    """The auth detail was previously cut at 'No api key pa'. With the
    200-char limit the full 'No api key passed' must survive into the
    stored detail."""
    from openclaw_audit import analyze

    entries = [("litellm", "2026-06-20T22:25:03+07:00", "ERROR", AUTH_MSG)]
    result = analyze(entries)
    detail = result["raw_events"][0]["detail"]
    assert "No api key passed" in detail
    assert not detail.endswith("No api key pa")


def test_litellm_err_log_skips_text_format_lines():
    """Legacy `HH:MM:SS - LiteLLM ...:` text-format lines carry no date
    and were previously mis-stamped as 'today', drifting historical events
    into the future. The parser must skip them entirely — only JSON lines
    (JSON_LOGS=true) with a full timestamp are parsed."""
    import tempfile, os
    from openclaw_audit import parsing

    lines = [
        # legacy text line, no date — must be skipped
        "22:25:03 - LiteLLM Router:ERROR: auth_exception_handler.py:97 - "
        "No api key passed in.\n",
        # JSON line with real date — must parse
        '{"message": "litellm.proxy.proxy_server.user_api_key_auth(): '
        'Exception occured - No api key passed in.", "level": "ERROR", '
        '"timestamp": "2026-06-20T22:25:57.000000", '
        '"component": "LiteLLM Proxy", "logger": "auth_exception_handler.py:97"}\n',
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as tf:
        tf.writelines(lines)
        path = tf.name
    try:
        orig = parsing.LITELLM_ERR_LOG
        parsing.LITELLM_ERR_LOG = path
        entries = parsing.parse_litellm_err_log(since=None)
    finally:
        parsing.LITELLM_ERR_LOG = orig
        os.unlink(path)

    # Only the JSON line must be parsed; the text line skipped.
    assert len(entries) == 1
    src, ts, level, msg = entries[0]
    assert src == "litellm"
    assert ts.startswith("2026-06-20T22:25:57")
    assert "No api key passed" in msg


def test_litellm_err_log_ignores_ansi_text_lines():
    """litellm's launchd-redirected stderr can contain ANSI-coloured text
    lines from its early startup (before JSON_LOGS applies). These must
    not be silently mis-stamped — they must be skipped."""
    import tempfile, os
    from openclaw_audit import parsing

    lines = [
        "\033[92m22:25:57 - LiteLLM Proxy:ERROR\033[0m: "
        "auth_exception_handler.py:97 - No api key passed in.\n",
        '{"message": "ok", "level": "INFO", '
        '"timestamp": "2026-06-20T22:30:00.000000", '
        '"component": "LiteLLM", "logger": "utils.py:1"}\n',
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as tf:
        tf.writelines(lines)
        path = tf.name
    try:
        orig = parsing.LITELLM_ERR_LOG
        parsing.LITELLM_ERR_LOG = path
        entries = parsing.parse_litellm_err_log(since=None)
    finally:
        parsing.LITELLM_ERR_LOG = orig
        os.unlink(path)

    assert len(entries) == 1
    assert entries[0][3] == "[LiteLLM] utils.py:1 - ok"


def test_cli_event_list_shows_full_date_with_year(capsys):
    """Event timestamps in the CLI list must include the date (with year),
    not just HH:MM:SS. Guards against a regression where cross-day events
    became ambiguous. litellm err lines now carry an inferred date too."""
    from datetime import datetime
    from openclaw_audit import analyze, print_report

    # A litellm auth event with a real ISO timestamp (date + time + tz).
    entries = [
        ("litellm", "2026-06-20T22:25:57+07:00", "ERROR",
         "[Proxy] auth_exception_handler.py:97 - litellm.proxy.proxy_server."
         "user_api_key_auth(): Exception occured - No api key passed"),
    ]
    result = analyze(entries)
    print_report(result, sqlite_info={}, sessions_info=None)
    out = capsys.readouterr().out

    # The event row must render the full date incl. year, not bare HH:MM:SS.
    assert "2026-06-20 22:25:57" in out
    # And the auth root-cause text must frame it as a /models probe (A-plan
    # noise reduction), not a credential emergency.
    assert "/models" in out
    assert "不影响" in out


# ─── JSON log format (JSON_LOGS=true) ─────────────────────────────────
def _write_tmp_log(lines):
    import tempfile, os
    tf = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    tf.writelines(lines)
    tf.close()
    return tf.name


def test_litellm_json_log_parsed_with_real_date():
    """JSON_LOGS=true lines carry a full ISO timestamp with date+year. The
    parser must use it directly instead of inferring from HH:MM:SS. The
    ts suffix follows LOCAL_TZ (now auto-detected from system tz by
    default, or set via OPENCLAW_AUDIT_TZ); pin the env var so the test
    is deterministic regardless of the host's tz."""
    import os
    from openclaw_audit import parsing
    # Reload config with a pinned tz so the assertion is deterministic.
    # (Module-level LOCAL_TZ was computed at import time, so we set it
    # back to what parsing already captured; the test only checks that
    # parse_litellm_err_log appends tz_offset_str(LOCAL_TZ).)
    from openclaw_audit.config import LOCAL_TZ
    from openclaw_audit.util import tz_offset_str
    expected_suffix = tz_offset_str(LOCAL_TZ)

    line = (
        '{"message": "litellm.proxy.proxy_server.user_api_key_auth(): '
        'Exception occured - No api key passed in.", "level": "ERROR", '
        '"timestamp": "2026-06-20T22:25:57.123456", '
        '"component": "LiteLLM Proxy", "logger": "auth_exception_handler.py:97"}\n'
    )
    path = _write_tmp_log([line])
    try:
        orig = parsing.LITELLM_ERR_LOG
        parsing.LITELLM_ERR_LOG = path
        entries = parsing.parse_litellm_err_log(since=None)
    finally:
        parsing.LITELLM_ERR_LOG = orig
        import os; os.unlink(path)

    assert len(entries) == 1
    src, ts, level, msg = entries[0]
    assert src == "litellm"
    assert ts == f"2026-06-20T22:25:57{expected_suffix}", ts
    assert level == "ERROR"
    # auth substrings must survive into the detail so classify_litellm_entry
    # still buckets this as auth_error, not proxy_exception.
    assert "auth_exception_handler" in msg
    assert "user_api_key_auth" in msg
    assert "No api key passed" in msg


def test_litellm_json_log_classifies_as_auth_error():
    """End-to-end: a JSON auth line flows through analyze as auth_error."""
    from openclaw_audit import analyze, parsing
    line = (
        '{"message": "litellm.proxy.proxy_server.user_api_key_auth(): '
        'Exception occured - No api key passed in.", "level": "ERROR", '
        '"timestamp": "2026-06-20T22:25:57.000000", '
        '"component": "LiteLLM Proxy", "logger": "auth_exception_handler.py:97"}\n'
    )
    path = _write_tmp_log([line])
    try:
        orig = parsing.LITELLM_ERR_LOG
        parsing.LITELLM_ERR_LOG = path
        entries = parsing.parse_litellm_err_log(since=None)
    finally:
        parsing.LITELLM_ERR_LOG = orig
        import os; os.unlink(path)

    result = analyze(entries)
    assert result["litellm"]["auth_errors"] == 1
    assert result["litellm"]["proxy_exceptions"] == 0
    assert result["raw_events"][0]["type"] == "🔑 Litellm鉴权失败"


def test_litellm_json_and_text_lines_only_json_parsed():
    """A file may contain both JSON lines and legacy text lines (e.g. from
    a config flip mid-run). Only JSON lines must parse; text lines skipped."""
    from openclaw_audit import parsing
    lines = [
        # legacy text line, same day — must be skipped
        "22:25:03 - LiteLLM Router:ERROR: auth_exception_handler.py:97 - "
        "No api key passed in.\n",
        # JSON line a bit later
        '{"message": "something else", "level": "WARNING", '
        '"timestamp": "2026-06-20T22:40:07.000000", "component": "LiteLLM", '
        '"logger": "utils.py:768"}\n',
    ]
    path = _write_tmp_log(lines)
    try:
        orig = parsing.LITELLM_ERR_LOG
        parsing.LITELLM_ERR_LOG = path
        entries = parsing.parse_litellm_err_log(since=None)
    finally:
        parsing.LITELLM_ERR_LOG = orig
        import os; os.unlink(path)

    assert len(entries) == 1
    # JSON entry keeps its own real date.
    assert entries[0][1].startswith("2026-06-20T22:40:07")


# ─── LiteLLM log rotation ─────────────────────────────────────────


def test_rotate_litellm_logs_copytruncate():
    """rotate_litellm_logs must copy the current file to .1 and truncate
    the original in place, so the litellm process's open fd stays valid."""
    import tempfile, os
    from openclaw_audit import parsing

    with tempfile.TemporaryDirectory() as tmpdir:
        err_path = os.path.join(tmpdir, "litellm.err.log")
        with open(err_path, "w") as f:
            f.write("line1\nline2\n")
        orig_err = parsing.LITELLM_ERR_LOG
        orig_out = parsing.LITELLM_OUT_LOG
        parsing.LITELLM_ERR_LOG = err_path
        parsing.LITELLM_OUT_LOG = os.path.join(tmpdir, "litellm.out.log")
        try:
            results = parsing.rotate_litellm_logs(keep=3)
        finally:
            parsing.LITELLM_ERR_LOG = orig_err
            parsing.LITELLM_OUT_LOG = orig_out

        assert results[err_path]["rotated"] is True
        assert os.path.exists(f"{err_path}.1")
        with open(f"{err_path}.1") as f:
            assert f.read() == "line1\nline2\n"
        with open(err_path) as f:
            assert f.read() == ""


def test_rotate_shifts_existing_backups():
    """rotate must shift .1 -> .2, .2 -> .3, ..., and drop files beyond
    keep, so the backup count stays bounded."""
    import tempfile, os
    from openclaw_audit import parsing

    with tempfile.TemporaryDirectory() as tmpdir:
        err_path = os.path.join(tmpdir, "litellm.err.log")
        with open(err_path, "w") as f:
            f.write("current\n")
        # pre-existing backups
        for i, content in enumerate([".1 content", ".2 content", ".3 content"], start=1):
            with open(f"{err_path}.{i}", "w") as f:
                f.write(content)
        orig_err = parsing.LITELLM_ERR_LOG
        orig_out = parsing.LITELLM_OUT_LOG
        parsing.LITELLM_ERR_LOG = err_path
        parsing.LITELLM_OUT_LOG = os.path.join(tmpdir, "litellm.out.log")
        try:
            results = parsing.rotate_litellm_logs(keep=3)
        finally:
            parsing.LITELLM_ERR_LOG = orig_err
            parsing.LITELLM_OUT_LOG = orig_out

        assert results[err_path]["rotated"] is True
        # .3 dropped (beyond keep), .2 -> .3, .1 -> .2, current -> .1
        assert not os.path.exists(f"{err_path}.4")
        with open(f"{err_path}.1") as f:
            assert f.read() == "current\n"
        with open(f"{err_path}.2") as f:
            assert f.read() == ".1 content"
        with open(f"{err_path}.3") as f:
            assert f.read() == ".2 content"


def test_maybe_rotate_skips_small_files():
    """maybe_rotate_litellm_logs must skip files below the size threshold."""
    import tempfile, os
    from openclaw_audit import parsing

    with tempfile.TemporaryDirectory() as tmpdir:
        err_path = os.path.join(tmpdir, "litellm.err.log")
        with open(err_path, "w") as f:
            f.write("small\n")
        orig_err = parsing.LITELLM_ERR_LOG
        orig_out = parsing.LITELLM_OUT_LOG
        parsing.LITELLM_ERR_LOG = err_path
        parsing.LITELLM_OUT_LOG = os.path.join(tmpdir, "litellm.out.log")
        try:
            results = parsing.maybe_rotate_litellm_logs(max_size_bytes=1024)
        finally:
            parsing.LITELLM_ERR_LOG = orig_err
            parsing.LITELLM_OUT_LOG = orig_out

        assert results[err_path]["rotated"] is False
        assert "size=" in results[err_path]["reason"]
        # original file untouched
        with open(err_path) as f:
            assert f.read() == "small\n"


def test_maybe_rotate_triggers_on_oversize():
    """maybe_rotate_litellm_logs must rotate when file >= threshold."""
    import tempfile, os
    from openclaw_audit import parsing

    with tempfile.TemporaryDirectory() as tmpdir:
        err_path = os.path.join(tmpdir, "litellm.err.log")
        with open(err_path, "w") as f:
            f.write("x" * 200)
        orig_err = parsing.LITELLM_ERR_LOG
        orig_out = parsing.LITELLM_OUT_LOG
        parsing.LITELLM_ERR_LOG = err_path
        parsing.LITELLM_OUT_LOG = os.path.join(tmpdir, "litellm.out.log")
        try:
            results = parsing.maybe_rotate_litellm_logs(max_size_bytes=100)
        finally:
            parsing.LITELLM_ERR_LOG = orig_err
            parsing.LITELLM_OUT_LOG = orig_out

        assert results[err_path]["rotated"] is True
        assert os.path.exists(f"{err_path}.1")


def test_rotate_missing_file_is_skipped():
    """rotate must not crash when a log file is missing; it returns
    rotated=False with reason='missing'."""
    import tempfile, os
    from openclaw_audit import parsing

    with tempfile.TemporaryDirectory() as tmpdir:
        orig_err = parsing.LITELLM_ERR_LOG
        orig_out = parsing.LITELLM_OUT_LOG
        parsing.LITELLM_ERR_LOG = os.path.join(tmpdir, "missing.err.log")
        parsing.LITELLM_OUT_LOG = os.path.join(tmpdir, "missing.out.log")
        try:
            results = parsing.rotate_litellm_logs(keep=3)
        finally:
            parsing.LITELLM_ERR_LOG = orig_err
            parsing.LITELLM_OUT_LOG = orig_out

        for path, r in results.items():
            assert r["rotated"] is False
            assert r["reason"] == "missing"


# ─── OpenClaw level path + new event types ───────────────────────────
# Fixtures use the real field shape captured from the live log on
# 172.27.15.62: logLevelName lives under _meta (the top-level field is
# absent), and message/time are top-level. Built with json.dumps so the
# nested-escaping matches what the process actually writes.


def _openclaw_line(message, level, time_iso):
    import json as _json
    d = {
        "0": "{\"subsystem\":\"telegram/send\"}",
        "1": message,
        "_meta": {
            "logLevelName": level,
            "name": "{\"subsystem\":\"telegram/send\"}",
            "date": "2026-06-20T16:51:48.689Z",
            "path": {"fileName": "subsystem-BbA2Znit.js"},
        },
        "time": time_iso,
        "message": message,
        "traceId": "b877dabd816b6d6b60d121f2f5b62a8c",
    }
    return _json.dumps(d, ensure_ascii=False) + chr(10)


def test_openclaw_level_read_from_meta():
    """Real OpenClaw logs carry logLevelName under _meta, not at the top
    level. The parser must look there too, or every line's level comes back
    empty and all WARN/ERROR events get dropped as 'other'."""
    from openclaw_audit import parsing
    line = _openclaw_line(
        "failed to mirror outbound delivery into session transcript; "
        "channel send already succeeded: session file changed while embedded "
        "prompt lock was released: /Users/rosen/.openclaw/agents/main/sessions/x.jsonl",
        "WARN",
        "2026-06-20T23:51:48.706+07:00",
    )
    path = _write_tmp_log([line])
    try:
        orig = parsing.LOG_DIR
        # parse_openclaw_log takes an explicit filepath, so call it directly.
        entries = parsing.parse_openclaw_log(path)
    finally:
        import os; os.unlink(path)

    assert len(entries) == 1
    _src, _ts, level, _msg = entries[0]
    assert level == "WARN", f"level must be read from _meta.logLevelName, got {level!r}"


def test_transcript_mirror_failed_classified_and_counted():
    """The 'failed to mirror ... session transcript' WARN must get its own
    category, count, and event entry instead of being buried as generic WARN."""
    from openclaw_audit import analyze, classify_entry
    msg = (
        "failed to mirror outbound delivery into session transcript; "
        "channel send already succeeded: session file changed while embedded "
        "prompt lock was released: /Users/rosen/.openclaw/agents/main/sessions/x.jsonl"
    )
    cat = classify_entry(msg, "WARN")
    assert cat["type"] == "transcript_mirror_failed"

    entries = [("openclaw", "2026-06-20T23:51:48.706+07:00", "WARN", msg)]
    result = analyze(entries)
    assert result["transcript_mirror_failures"] == 1
    assert result["summary"]["transcript_mirror_failures"] == 1
    ev = result["raw_events"][0]
    assert ev["type"] == "📝 会话记录缺失"
    assert ev["level"] == "WARN"


def test_takeover_error_silent_gap_classified_and_counted():
    """The 'lane task error: ... EmbeddedAttemptSessionTakeoverError: ...' ERROR
    must get its own category because it produces a silent transcript gap —
    the mirror call is never reached, so the regular `transcript_mirror_failed`
    WARN does NOT fire. Without this classifier the gap would be buried as
    generic ERROR noise."""
    from openclaw_audit import analyze, classify_entry
    msg = (
        'lane task error: lane=session:agent:main:telegram:direct:670530854 '
        'durationMs=91506 error="EmbeddedAttemptSessionTakeoverError: session '
        'file changed while embedded prompt lock was released: '
        '/Users/rosen/.openclaw/agents/main/sessions/x.jsonl"'
    )
    cat = classify_entry(msg, "ERROR")
    assert cat["type"] == "takeover_error_silent_gap"

    entries = [("openclaw", "2026-06-26T16:29:09.279+08:00", "ERROR", msg)]
    result = analyze(entries)
    assert result["takeover_silent_gaps"] == 1
    assert result["summary"]["takeover_silent_gaps"] == 1
    ev = result["raw_events"][0]
    assert ev["type"] == "🔇 静默会话记录缺失"
    assert ev["level"] == "ERROR"

    # Ensure the regular transcript_mirror_failed path still works and is
    # NOT double-counted as a silent gap (the WARN message contains
    # "EmbeddedAttemptSessionTakeoverError" inside its `reason` string but
    # must classify as transcript_mirror_failed, not takeover_error_silent_gap).
    warn_msg = (
        "failed to mirror outbound delivery into session transcript; "
        "channel send already succeeded: session file changed while embedded "
        "prompt lock was released: /Users/rosen/.openclaw/agents/main/sessions/x.jsonl"
    )
    warn_cat = classify_entry(warn_msg, "WARN")
    assert warn_cat["type"] == "transcript_mirror_failed"


def test_truncate_helper_marks_truncation():
    """`_truncate` must append an ellipsis when the input exceeds the limit
    so a clipped path/error message is not mistaken for the full string.
    Inputs at or below the limit pass through unchanged."""
    from openclaw_audit import _truncate

    assert _truncate("short", 10) == "short"
    assert _truncate("exactly10", 10) == "exactly10"
    # 11 chars truncated to 10 leaves room for the 1-char marker (9 head + 1 marker).
    out = _truncate("elevenchars", 10)
    assert len(out) == 10
    assert out.endswith("…")
    assert out.startswith("elevencha")  # original 9-char head preserved
    # Custom marker is honored.
    out2 = _truncate("elevenchars", 10, marker="...")
    assert out2.endswith("...")
    assert len(out2) == 10
    # None passes through unchanged.
    assert _truncate(None, 10) is None


def test_analyze_preserves_long_error_detail_with_ellipsis():
    """Long log messages used to be cut at 200 chars with no marker, hiding
    the tail (file paths, provider names, error chains). The detail must now
    retain ~400 chars and end with an ellipsis when clipped. Pick a generic
    WARN that falls through to the `other` branch (which uses _truncate on
    the raw msg) rather than a classifier that emits a fixed detail string."""
    from openclaw_audit import analyze

    long_msg = (
        "[plugins] plugins.allow is empty; discovered non-bundled plugins may "
        "auto-load: codex (/Users/rosen/.openclaw/npm/projects/openclaw-codex"
        "-8902d781d4/node_modules/@openclaw/codex/dist/index.js). Set "
        "plugins.allow to explicit trusted ids. "
        + "x" * 250
        + " tail_that_must_survive=1"
    )
    # Sanity: the message is over the 400-char ceiling so truncation kicks in.
    assert len(long_msg) > 400
    entries = [("openclaw", "2026-06-26T08:39:38.442+08:00", "WARN", long_msg)]
    result = analyze(entries)
    # Find the raw_event that carries this message's detail.
    matching = [ev for ev in result["raw_events"] if "plugins.allow" in ev.get("detail", "")]
    assert matching, "expected at least one raw_event carrying the long plugins WARN"
    detail = matching[0]["detail"]
    assert len(detail) <= 400, "detail must not exceed the 400-char ceiling"
    assert detail.endswith("…"), "clipped detail must end with the ellipsis marker"


def test_telegram_send_ok_classified_not_double_counted():
    """'telegram outbound send ok' is the high-frequency delivery line. It
    must get its own type and show in events, but NOT increment
    telegram.outbound (that path is the legacy 'message processed' line;
    counting both would double-count until co-occurrence is ruled out)."""
    from openclaw_audit import analyze, classify_entry
    msg = (
        "telegram outbound send ok accountId=default chatId=670530854 "
        "messageId=1955 operation=sendRichMessage deliveryKind=text chunkCount=1"
    )
    cat = classify_entry(msg, "INFO")
    assert cat["type"] == "telegram_send_ok"
    assert cat["message_id"] == "1955"
    assert cat["chat_id"] == "670530854"

    entries = [("openclaw", "2026-06-20T23:51:48.689+07:00", "INFO", msg)]
    result = analyze(entries)
    assert result["telegram"]["send_ok"] == 1
    # outbound must NOT include send_ok
    assert result["telegram"]["outbound"] == 0
    assert result["summary"]["telegram_send_ok"] == 1
    ev = result["raw_events"][0]
    assert ev["type"] == "📤 Telegram回复成功"
    assert ev["level"] == "INFO"


def test_telegram_send_ok_new_sendrichmessage_format():
    """The channels/telegram direct-send path logs a different wording:
    'telegram sendRichMessage ok chat=... message=...' (no 'outbound',
    field is 'message=' not 'messageId=', 'chat=' not 'chatId='). This
    must also classify as telegram_send_ok so direct sends are counted.
    Verified mutually exclusive with the queued-send path (disjoint
    messageId sets across 06-20/06-21 logs), so no double-count risk."""
    from openclaw_audit import analyze, classify_entry
    msg = "telegram sendRichMessage ok chat=670530854 message=1964"
    cat = classify_entry(msg, "INFO")
    assert cat["type"] == "telegram_send_ok"
    assert cat["message_id"] == "1964"
    assert cat["chat_id"] == "670530854"

    entries = [("openclaw", "2026-06-21T10:48:31.839+07:00", "INFO", msg)]
    result = analyze(entries)
    assert result["telegram"]["send_ok"] == 1
    assert result["telegram"]["outbound"] == 0
    ev = result["raw_events"][0]
    assert ev["type"] == "📤 Telegram回复成功"
    assert ev["detail"] == "messageId=1964"


def test_telegram_send_ok_both_paths_counted_no_double_count():
    """Both send paths (queued 'outbound send ok' + direct 'sendRichMessage
    ok') emit separate lines with separate messageIds for separate sends.
    A log containing one of each must count send_ok=2, not collapse them."""
    from openclaw_audit import analyze
    entries = [
        ("openclaw", "2026-06-21T10:48:31.839+07:00", "INFO",
         "telegram sendRichMessage ok chat=670530854 message=1964"),
        ("openclaw", "2026-06-21T10:48:32.000+07:00", "INFO",
         "telegram outbound send ok accountId=default chatId=670530854 "
         "messageId=1965 operation=sendRichMessage deliveryKind=text"),
    ]
    result = analyze(entries)
    assert result["telegram"]["send_ok"] == 2
    assert result["summary"]["telegram_send_ok"] == 2


def test_warn_no_longer_silently_dropped_after_level_fix():
    """Before the level-path fix, a generic WARN with no specific category
    landed in 'other' (empty level) and never reached raw_events. With the
    fix it must surface as an unknown_error event so real WARNs are visible."""
    from openclaw_audit import analyze
    # A WARN that matches no specific category -> unknown_error bucket
    msg = "skipped permission hardening for /Users/rosen/something"
    entries = [("openclaw", "2026-06-20T23:00:00.000+07:00", "WARN", msg)]
    result = analyze(entries)
    assert len(result["other_errors"]) == 1
    ev = result["raw_events"][0]
    assert ev["level"] == "WARN"
    assert ev["type"].startswith("❌")


# ─── parse_ts: negative tz offset + tz suffix handling (Fix #5) ────────
def test_parse_ts_accepts_negative_offset():
    """ISO timestamps with a negative tz offset (e.g. -05:00) must parse
    instead of falling through to ValueError. The offset is stripped and
    re-stamped with LOCAL_TZ (existing behavior for + offsets)."""
    from openclaw_audit import parse_ts

    dt = parse_ts("2026-06-18T07:43:06.757-05:00")
    assert dt is not None
    # Original wall-clock time preserved, but tzinfo replaced with LOCAL_TZ
    assert dt.strftime("%H:%M:%S") == "07:43:06"


def test_parse_ts_still_parses_positive_offset():
    """Regression: positive offsets must still parse (was the only supported
    case before the negative-offset fix)."""
    from openclaw_audit import parse_ts

    dt = parse_ts("2026-06-18T07:43:06.757+07:00")
    assert dt is not None
    assert dt.strftime("%H:%M:%S") == "07:43:06"


def test_parse_ts_still_parses_z_suffix():
    """Regression: Z (UTC) suffix must still parse."""
    from openclaw_audit import parse_ts

    dt = parse_ts("2026-06-18T07:43:06.757Z")
    assert dt is not None
    assert dt.strftime("%H:%M:%S") == "07:43:06"


def test_parse_ts_naive_no_offset_uses_local_tz():
    """Regression: a timestamp with no tz suffix is parsed and stamped
    with LOCAL_TZ (litellm JSON logs look like this)."""
    from openclaw_audit import parse_ts

    dt = parse_ts("2026-06-18T07:43:06.757")
    assert dt is not None
    assert dt.tzinfo is not None  # was naive before, caused timestamp() drift


# ─── tz_offset_str (Fix #2) ───────────────────────────────────────────
def test_tz_offset_str_renders_local_tz():
    """tz_offset_str must render LOCAL_TZ as ±HH:MM so LiteLLM ts strings
    don't carry a hardcoded +07:00. LOCAL_TZ is auto-detected from the
    system by default (or set via OPENCLAW_AUDIT_TZ), so the assertion
    just verifies the format matches LOCAL_TZ's actual offset."""
    from openclaw_audit.util import tz_offset_str
    from openclaw_audit.config import LOCAL_TZ

    s = tz_offset_str(LOCAL_TZ)
    # Format check: ±HH:MM with leading zeros, not +7:00 or +07:0
    import re
    assert re.match(r"^[+-]\d{2}:\d{2}$", s), (
        f"expected ±HH:MM format, got {s!r}"
    )
    # Cross-check against LOCAL_TZ's actual offset
    expected_offset = LOCAL_TZ.utcoffset(None)
    if expected_offset is not None:
        total = int(expected_offset.total_seconds())
        sign = "+" if total >= 0 else "-"
        h, rem = divmod(abs(total), 3600)
        m = rem // 60
        assert s == f"{sign}{h:02d}:{m:02d}", f"offset mismatch: {s}"


def test_tz_offset_str_utc():
    from openclaw_audit.util import tz_offset_str
    from datetime import timezone

    assert tz_offset_str(timezone.utc) == "+00:00"


def test_tz_offset_str_half_hour():
    """Half-hour tz offsets must render as ±HH:MM with minutes (not seconds).
    Guards against a divmod-on-seconds bug that printed +05:1800 instead of
    +05:30."""
    from openclaw_audit.util import tz_offset_str
    from datetime import timedelta, timezone

    tz = timezone(timedelta(hours=5, minutes=30))
    assert tz_offset_str(tz) == "+05:30"

    tz_neg = timezone(timedelta(hours=-9, minutes=-30))
    assert tz_offset_str(tz_neg) == "-09:30"


# ─── config.parse_tz_str: half-hour / negative / UTC (Fix #6) ───────────
def test_parse_tz_str_half_hour_offset():
    """+05:30 (India) must parse including the 30-minute component —
    previously int(s[:3]) dropped the minutes."""
    from openclaw_audit.config import parse_tz_str
    from datetime import timedelta

    tz = parse_tz_str("+05:30")
    assert tz.utcoffset(None) == timedelta(hours=5, minutes=30)


def test_parse_tz_str_negative_offset():
    from openclaw_audit.config import parse_tz_str
    from datetime import timedelta

    tz = parse_tz_str("-05:00")
    assert tz.utcoffset(None) == timedelta(hours=-5)


def test_parse_tz_str_utc_keyword():
    from openclaw_audit.config import parse_tz_str
    from datetime import timezone

    assert parse_tz_str("UTC") == timezone.utc
    assert parse_tz_str("utc") == timezone.utc


def test_parse_tz_str_compact_form():
    """+0700 (no colon) and +07 (hours only) should also parse."""
    from openclaw_audit.config import parse_tz_str
    from datetime import timedelta

    assert parse_tz_str("+0700").utcoffset(None) == timedelta(hours=7)
    assert parse_tz_str("+07").utcoffset(None) == timedelta(hours=7)


def test_parse_tz_str_invalid_raises():
    import pytest
    from openclaw_audit.config import parse_tz_str

    with pytest.raises(ValueError):
        parse_tz_str("garbage")


# ─── Auto-detect system tz (no OPENCLAW_AUDIT_TZ) ─────────────────────
def test_detect_system_tz_returns_timezone_with_offset():
    """_detect_system_tz must return a tzinfo whose utcoffset is non-None
    and matches the actual system offset (computed independently via
    datetime.now().astimezone()). Guards against a regression where the
    fallback path returned a naive tz."""
    from datetime import datetime
    from openclaw_audit.config import _detect_system_tz

    tz = _detect_system_tz()
    assert tz is not None
    offset = tz.utcoffset(None)
    assert offset is not None, "_detect_system_tz must return a tz with a known offset"
    # Cross-check: the offset must equal what datetime.now().astimezone() returns
    expected = datetime.now().astimezone().utcoffset()
    assert offset == expected, f"detected offset {offset} != system {expected}"


def test_local_tz_uses_env_override_when_set():
    """When OPENCLAW_AUDIT_TZ is set, it must win over system auto-detect.
    Runs in a subprocess so the env var doesn't leak into other tests."""
    import os
    import subprocess
    import sys

    code = (
        "from openclaw_audit.config import LOCAL_TZ; "
        "from openclaw_audit.util import tz_offset_str; "
        "print(tz_offset_str(LOCAL_TZ))"
    )
    env = os.environ.copy()
    env["OPENCLAW_AUDIT_TZ"] = "-05:00"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        cwd=str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "-05:00", (
        f"env override should win, got {result.stdout!r}"
    )


def test_local_tz_auto_detects_when_env_unset():
    """When OPENCLAW_AUDIT_TZ is NOT set, LOCAL_TZ must auto-detect from
    the system (not fall back to a hardcoded +07:00). Runs in subprocess
    with the env var explicitly unset."""
    import os
    import subprocess
    import sys
    from datetime import datetime

    code = (
        "from openclaw_audit.config import LOCAL_TZ; "
        "from openclaw_audit.util import tz_offset_str; "
        "print(tz_offset_str(LOCAL_TZ))"
    )
    env = os.environ.copy()
    env.pop("OPENCLAW_AUDIT_TZ", None)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        cwd=str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    assert result.returncode == 0, result.stderr
    detected = result.stdout.strip()
    # Must match the actual system offset (not a hardcoded +07:00).
    expected_offset = datetime.now().astimezone().utcoffset()
    total = int(expected_offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    h, rem = divmod(abs(total), 3600)
    m = rem // 60
    expected_str = f"{sign}{h:02d}:{m:02d}"
    assert detected == expected_str, (
        f"auto-detected tz should match system ({expected_str}), got {detected}"
    )


def test_local_tz_no_hardcoded_default_when_env_unset():
    """Specifically: with OPENCLAW_AUDIT_TZ unset, LOCAL_TZ must NOT be
    hardcoded +07:00 unless the system actually is in +07. Guards the
    original bug where the default was hardcoded +07:00 and a +08:00 /
    -05:00 system would still show +07:00 in the dashboard."""
    import os
    import subprocess
    import sys
    from datetime import datetime

    code = (
        "from openclaw_audit.config import LOCAL_TZ; "
        "from openclaw_audit.util import tz_offset_str; "
        "print(tz_offset_str(LOCAL_TZ))"
    )
    env = os.environ.copy()
    env.pop("OPENCLAW_AUDIT_TZ", None)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        cwd=str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    assert result.returncode == 0, result.stderr
    detected = result.stdout.strip()
    system_offset = datetime.now().astimezone().utcoffset()
    total = int(system_offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    h, rem = divmod(abs(total), 3600)
    m = rem // 60
    expected = f"{sign}{h:02d}:{m:02d}"
    # If system happens to be +07:00, the test would pass trivially; that's
    # fine. The point is that it's not the hardcoded constant — it tracks
    # the system tz.
    assert detected == expected, (
        f"LOCAL_TZ should track system tz ({expected}), got {detected!r}"
    )


# ─── parse_since_arg: today/yesterday (Fix #3) ────────────────────────
def test_parse_since_arg_today_does_not_raise():
    """The bug: '--since today' used to fall into the YYYY-MM-DD else branch
    and sys.exit(1). Now it returns today's midnight in LOCAL_TZ."""
    from openclaw_audit.util import parse_since_arg

    since, label = parse_since_arg("today")
    assert label == "今天"
    assert since.hour == 0 and since.minute == 0


def test_parse_since_arg_yesterday():
    from openclaw_audit.util import parse_since_arg

    since, label = parse_since_arg("yesterday")
    assert label == "昨天"
    assert since.hour == 0 and since.minute == 0


def test_parse_since_arg_default_1h():
    from openclaw_audit.util import parse_since_arg, now_local
    from datetime import timedelta

    since, label = parse_since_arg("")
    assert label == "最近 1 小时"
    assert since <= now_local() - timedelta(hours=1, minutes=-1)


def test_parse_since_arg_invalid_raises():
    import pytest
    from openclaw_audit.util import parse_since_arg

    with pytest.raises(ValueError):
        parse_since_arg("not-a-date")


def test_parse_since_arg_hours_and_days():
    from openclaw_audit.util import parse_since_arg

    since_h, label_h = parse_since_arg("3h")
    assert label_h == "最近 3 小时"
    since_d, label_d = parse_since_arg("2d")
    assert label_d == "最近 2 天"


# ─── parsing mtime cache (Fix #7) ────────────────────────────────────
def test_parsing_cache_skips_unchanged_file(tmp_path):
    """In watch mode the same log file is re-parsed every interval. When
    mtime and size are unchanged the cached entries must be returned
    without re-reading the file."""
    from openclaw_audit import parsing

    log = tmp_path / "openclaw-test.log"
    log.write_text(
        '{"message":"hello","time":"2026-06-20T22:25:57+07:00",'
        '"_meta":{"logLevelName":"INFO"}}\n'
    )

    parsing._FILE_CACHE.clear()

    call_count = [0]
    orig = parsing._parse_openclaw_log_raw

    def counting(filepath):
        call_count[0] += 1
        return orig(filepath)

    parsing._parse_openclaw_log_raw = counting
    try:
        entries1 = parsing.parse_openclaw_log(str(log))
        assert call_count[0] == 1
        assert len(entries1) == 1

        # Second call: same mtime/size -> cache hit, raw parser NOT called.
        entries2 = parsing.parse_openclaw_log(str(log))
        assert call_count[0] == 1
        assert entries1 == entries2
    finally:
        parsing._parse_openclaw_log_raw = orig


def test_parsing_cache_invalidates_on_change(tmp_path):
    """When the file's mtime/size change, the cache must miss and the file
    must be re-parsed (otherwise watch mode would never see new log lines)."""
    import os
    from openclaw_audit import parsing

    log = tmp_path / "openclaw-test.log"
    log.write_text(
        '{"message":"first","time":"2026-06-20T22:25:57+07:00",'
        '"_meta":{"logLevelName":"INFO"}}\n'
    )

    parsing._FILE_CACHE.clear()
    entries1 = parsing.parse_openclaw_log(str(log))
    assert entries1[0][3] == "first"

    # Modify content + bump mtime so the cache key changes.
    log.write_text(
        '{"message":"second","time":"2026-06-20T22:25:58+07:00",'
        '"_meta":{"logLevelName":"INFO"}}\n'
    )
    new_mtime = os.path.getmtime(str(log)) + 5
    os.utime(str(log), (new_mtime, new_mtime))

    entries2 = parsing.parse_openclaw_log(str(log))
    assert entries2[0][3] == "second"


# ─── LiteLLM ts suffix follows LOCAL_TZ, not hardcoded +07:00 (Fix #2) ─
def test_litellm_json_log_ts_uses_local_tz_offset(tmp_path):
    """End-to-end: when OPENCLAW_AUDIT_TZ is overridden, the ts suffix on
    LiteLLM JSON entries must follow LOCAL_TZ. Was hardcoded +07:00."""
    import os
    import subprocess

    log = tmp_path / "litellm.err.log"
    log.write_text(
        '{"message":"x","level":"ERROR","timestamp":"2026-06-20T22:25:57.000000",'
        '"component":"L","logger":"f.py:1"}\n'
    )

    env = os.environ.copy()
    env["OPENCLAW_AUDIT_TZ"] = "-05:00"
    env["LITELLM_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(os.path.dirname(__file__))  # so `openclaw_audit` resolves

    result = subprocess.run(
        ["python3", "-c",
         "from openclaw_audit.parsing import parse_litellm_err_log; "
         "print(parse_litellm_err_log()[0][1])"],
        capture_output=True, text=True, env=env,
        cwd=str(os.path.dirname(os.path.dirname(__file__))),
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    ts = result.stdout.strip()
    assert ts.endswith("-05:00"), f"ts suffix must follow LOCAL_TZ, got {ts}"


def test_report_generation_time_uses_local_tz_offset(capsys, monkeypatch):
    """The '生成时间' line in the CLI report must show the LOCAL_TZ offset,
    not a hardcoded '+07:00'. Guards against a missed third hardcoded
    location found during e2e verification on 172.27.15.62."""
    import os
    import subprocess
    from datetime import timedelta, timezone

    # Run in a subprocess so we can override OPENCLAW_AUDIT_TZ cleanly
    # without leaking into other tests via module-level LOCAL_TZ caching.
    code = (
        "from openclaw_audit import analyze, print_report; "
        "r = analyze([]); "
        "print_report(r, {}, None)"
    )
    env = os.environ.copy()
    env["OPENCLAW_AUDIT_TZ"] = "-05:00"
    result = subprocess.run(
        ["python3", "-c", code],
        capture_output=True, text=True, env=env,
        cwd=str(os.path.dirname(os.path.dirname(__file__))),
    )
    assert result.returncode == 0, result.stderr
    assert "(-05:00)" in result.stdout, (
        f"generation time must show -05:00 suffix, got: {result.stdout!r}"
    )
    assert "(+07:00)" not in result.stdout, "hardcoded +07:00 leaked into report"


# ─── Web: format_latency + format_event_time (Fix B1, B2) ────────────
def test_format_latency_renders_units():
    """Web cards must show latency with units (12.3s, 500ms, 2m0s) instead
    of the raw float the previous Jinja template rendered."""
    from openclaw_audit.render import format_latency

    assert format_latency(None) == "N/A"
    assert format_latency(0.5) == "500ms"
    assert format_latency(12.3) == "12.3s"
    assert format_latency(95.7) == "1m35s"
    assert format_latency(120) == "2m0s"


def test_format_event_time_handles_iso_text_and_empty():
    """Web event row must format ISO timestamps as 'YYYY-MM-DD HH:MM:SS'
    instead of the naive ``ev.time[:19].replace('T', ' ')`` that broke for
    text-format LiteLLM lines (no date) and any non-ISO shape."""
    from openclaw_audit.render import format_event_time

    assert format_event_time(None) == "??"
    assert format_event_time("") == "??"
    # ISO with offset: stripped, re-stamped with LOCAL_TZ, formatted
    iso = format_event_time("2026-06-20T22:25:57.000+07:00")
    assert iso == "2026-06-20 22:25:57", iso
    # text HH:MM:SS: parses using today's date
    text = format_event_time("22:25:57")
    assert text.endswith(" 22:25:57"), text


# ─── Web: render_section_fragments (Fix A) ─────────────────────────────
def test_render_section_fragments_returns_all_sections():
    """The /api/fragments endpoint must return all 7 section IDs that the
    JS expects to patch. Adding/removing a section without updating both
    sides silently breaks refresh."""
    from openclaw_audit.render import render_section_fragments
    from openclaw_audit import analyze

    result = analyze([])
    fragments = render_section_fragments(result, ["✅ ok"], {"active": []})

    expected = {
        "stats-openclaw", "stats-litellm", "stats-latency",
        "stats-sessions", "time-series", "suggestions", "event-list",
    }
    assert set(fragments.keys()) == expected, (
        f"missing sections: {expected - set(fragments.keys())}, "
        f"extra: {set(fragments.keys()) - expected}"
    )


def test_render_section_fragments_latency_uses_units():
    """Latency in fragments must be pre-formatted with units (was raw float)."""
    from openclaw_audit.render import render_section_fragments
    from openclaw_audit import analyze

    entries = [
        ("openclaw", "2026-06-20T16:35:00.000+07:00", "ERROR",
         "model-fetch error provider=litellm elapsedMs=12300 AbortError: aborted"),
    ]
    result = analyze(entries)
    fragments = render_section_fragments(result, [], {"active": []})
    assert "12.3s" in fragments["stats-latency"], (
        f"expected '12.3s' in latency fragment, got: {fragments['stats-latency']!r}"
    )


# ─── Web: /api/fragments and /api/data endpoints (Fix A, C, E) ───────
# Flask is an optional dependency (AGENTS.md). Skip these integration tests
# when it isn't installed — they exercise the real HTTP endpoints.
try:
    import flask as _flask  # noqa: F401
    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

_FLASK_SKIP = pytest.mark.skipif(not _HAS_FLASK, reason="Flask not installed (optional)")


def _start_web_server(env_overrides=None):
    """Start the web server in a subprocess; return (proc, base_url)."""
    import os
    import subprocess
    import sys
    import time

    env = os.environ.copy()
    env["OPENCLAW_LOG_DIR"] = "/tmp/no-such-dir"
    env["LITELLM_DIR"] = "/tmp/no-such-dir"
    if env_overrides:
        env.update(env_overrides)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen(
        [sys.executable, "openclaw-audit.py", "--web", "--port", "19100"],
        cwd=repo_root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for server to be ready (max ~3s)
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen("http://127.0.0.1:19100/", timeout=0.5)
            return proc, "http://127.0.0.1:19100"
        except Exception:
            time.sleep(0.1)
    proc.terminate()
    return None, None


def _stop_web_server(proc):
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@_FLASK_SKIP
def test_api_fragments_endpoint_returns_sections():
    """The /api/fragments endpoint must return JSON with each section's
    pre-rendered HTML plus a generated_at timestamp. This is the contract
    the fetch-based refresh relies on."""
    import urllib.request
    import json

    proc, base = _start_web_server()
    try:
        assert proc is not None, "web server failed to start"
        with urllib.request.urlopen(f"{base}/api/fragments?since=1h", timeout=5) as r:
            assert r.status == 200
            d = json.loads(r.read())
        assert "error" in d and d["error"] is None
        assert "generated_at" in d and d["generated_at"]
        for k in ["stats-openclaw", "stats-litellm", "stats-latency",
                  "stats-sessions", "time-series", "suggestions", "event-list"]:
            assert k in d, f"missing section: {k}"
            assert isinstance(d[k], str)
    finally:
        _stop_web_server(proc)


@_FLASK_SKIP
def test_index_page_has_container_ids_for_fetch_refresh():
    """The initial HTML page must contain all the container IDs that
    /api/fragments returns, so JS can patch them by id."""
    import urllib.request

    proc, base = _start_web_server()
    try:
        assert proc is not None
        with urllib.request.urlopen(f"{base}/?since=1h", timeout=5) as r:
            html = r.read().decode()
        for cid in ["stats-openclaw", "stats-litellm", "stats-litellm",
                    "stats-latency", "stats-sessions", "time-series",
                    "suggestions", "event-list", "last-updated",
                    "error-banner", "refresh-btn"]:
            assert f'id="{cid}"' in html, f"missing container id: {cid}"
    finally:
        _stop_web_server(proc)


@_FLASK_SKIP
def test_api_data_endpoint_does_not_leak_sqlite_into_data_cache():
    """Regression: previously index() did `data["sqlite"] = sqlite_info`
    on the cached result, mutating it. The next request from cache would
    see the previous request's sqlite already attached. Now sqlite is
    carried separately, so two /api/data calls must not affect each
    other's data shape (the field is set fresh on each response)."""
    import urllib.request
    import json

    proc, base = _start_web_server()
    try:
        assert proc is not None
        # First call: should add sqlite to the response
        with urllib.request.urlopen(f"{base}/api/data?since=1h", timeout=5) as r:
            d1 = json.loads(r.read())
        assert "sqlite" in d1, "/api/data must include sqlite"

        # Second call within TTL: cached result, but sqlite is re-fetched
        # and re-attached. Verify the cached result payload itself wasn't
        # mutated to embed sqlite (would be a stale value if it was).
        with urllib.request.urlopen(f"{base}/api/data?since=1h", timeout=5) as r:
            d2 = json.loads(r.read())
        assert "sqlite" in d2
        # The two sqlite payloads should be equivalent (same cached value)
        # but the data dict should not have been mutated by the first call
        # in a way that breaks the second.
        assert d1["sqlite"] == d2["sqlite"]
    finally:
        _stop_web_server(proc)


# ─── SQLite failure surfacing (1a) + session-state badges (1b) ──────


def test_sqlite_recent_task_failures_surfaced():
    """query_sqlite must surface recent failed/lost task_runs with their
    error text so FailoverError / Codex limit are visible without reading
    raw logs. This test stubs the SQLite file with the production schema
    and verifies the failure rows are returned."""
    import tempfile, os, sqlite3
    from openclaw_audit import queries

    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "openclaw.sqlite")
        conn = sqlite3.connect(db_path)
        # Minimal schema matching production (only columns we read).
        conn.executescript("""
            CREATE TABLE task_runs (
                task_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                ended_at INTEGER, error TEXT, label TEXT
            );
        """)
        # A recent failure (age_min < 60) and an old one (> 60).
        import time
        now_ms = int(time.time() * 1000)
        conn.execute(
            "INSERT INTO task_runs VALUES (?,?,?,?,?)",
            ("recent-fail", "failed", now_ms - 5 * 60 * 1000,
             'FailoverError: No API key found for provider "openai"', "Review PR #80")
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (?,?,?,?,?)",
            ("old-fail", "failed", now_ms - 120 * 60 * 1000,
             "FailoverError: old error", "old task")
        )
        conn.commit()
        conn.close()

        orig = queries.SQLITE_DB
        queries.SQLITE_DB = db_path
        try:
            info = queries.query_sqlite()
        finally:
            queries.SQLITE_DB = orig

        assert "recent_task_failures" in info
        fails = info["recent_task_failures"]
        # Both rows returned (query doesn't filter by age, that's done in
        # suggestions); check the recent one is present with right fields.
        recent = [f for f in fails if f["status"] == "failed" and f["age_min"] <= 10]
        assert len(recent) == 1
        assert "No API key" in recent[0]["error"]
        assert recent[0]["label"] == "Review PR #80"


def test_sqlite_announce_failures_surfaced():
    """query_sqlite must surface subagent_runs with last_announce_delivery_error
    set — these are the wedged-parent events."""
    import tempfile, os, sqlite3, time
    from openclaw_audit import queries

    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "openclaw.sqlite")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE subagent_runs (
                run_id TEXT PRIMARY KEY,
                last_announce_delivery_error TEXT,
                announce_retry_count INTEGER,
                ended_reason TEXT,
                ended_at INTEGER
            );
        """)
        now_ms = int(time.time() * 1000)
        conn.execute(
            "INSERT INTO subagent_runs VALUES (?,?,?,?,?)",
            ("abc12345", "completion agent did not use the message tool",
             3, "subagent-error", now_ms - 10 * 60 * 1000)
        )
        conn.commit()
        conn.close()

        orig = queries.SQLITE_DB
        queries.SQLITE_DB = db_path
        try:
            info = queries.query_sqlite()
        finally:
            queries.SQLITE_DB = orig

        assert "recent_announce_failures" in info
        ann = info["recent_announce_failures"]
        assert len(ann) == 1
        assert ann[0]["run_id"] == "abc12345"
        assert ann[0]["retries"] == 3
        assert "message tool" in ann[0]["error"]


def test_suggestion_surfaces_failover_error_from_sqlite():
    """When sqlite_info has recent task_run failures with FailoverError,
    build_suggestions must surface a suggestion pointing at the API key
    issue rather than the generic 'system normal' message."""
    from openclaw_audit import analyze, build_suggestions

    result = analyze([])  # no log events → no session_stalls / llm_aborts
    result["sqlite_info"] = {
        "recent_task_failures": [
            {"status": "failed", "age_min": 5.0,
             "error": 'FailoverError: No API key found for provider "openai"',
             "label": "Review PR #80"},
            {"status": "failed", "age_min": 10.0,
             "error": 'FailoverError: No API key found for provider "openai"',
             "label": "Review PR #81"},
        ]
    }
    result["sessions_info"] = {"active": []}

    suggestions = build_suggestions(result, {"errors": 0})
    failover_sugs = [s for s in suggestions if "子Agent启动失败" in s or "FailoverError" in s]
    assert len(failover_sugs) == 1
    assert "API key" in failover_sugs[0]
    assert "近1h" in failover_sugs[0]


def test_suggestion_surfaces_codex_subscription_limit():
    """Codex subscription usage limit errors must surface with the reset
    time so the operator knows when the next attempt can happen."""
    from openclaw_audit import analyze, build_suggestions

    result = analyze([])
    result["sqlite_info"] = {
        "recent_task_failures": [
            {"status": "failed", "age_min": 5.0,
             "error": "Codex subscription usage limit. Next reset in 4 hours, Jun 27 at 12:31 PM",
             "label": ""},
        ]
    }
    result["sessions_info"] = {"active": []}

    suggestions = build_suggestions(result, {"errors": 0})
    codex_sugs = [s for s in suggestions if "Codex" in s or "订阅" in s]
    assert len(codex_sugs) == 1
    assert "4 hours" in codex_sugs[0]


def test_suggestion_announce_give_up_wedged_parent():
    """Recent subagent announce give-up failures must surface as a
    wedged-parent warning."""
    from openclaw_audit import analyze, build_suggestions

    result = analyze([])
    result["sqlite_info"] = {
        "recent_announce_failures": [
            {"run_id": "abc12345", "age_min": 8.0,
             "error": "completion agent did not use the message tool",
             "retries": 3, "ended_reason": "subagent-error"}
        ]
    }
    result["sessions_info"] = {"active": []}

    suggestions = build_suggestions(result, {"errors": 0})
    ann_sugs = [s for s in suggestions if "Announce give-up" in s or "wedged" in s]
    assert len(ann_sugs) == 1
    assert "wedged" in ann_sugs[0].lower()


def test_suggestion_context_window_new_at_80pct():
    """A direct session at >=80% context usage must trigger a /new
    suggestion (overflow imminent, /compact too late)."""
    from openclaw_audit import analyze, build_suggestions

    result = analyze([])
    result["sqlite_info"] = {}
    result["sessions_info"] = {
        "active": [{
            "kind": "direct", "model": "agnes-flash",
            "totalTokens": 210000, "contextTokens": 262144,
            "usagePct": 80.1, "hasTokens": True, "isFailed": False,
            "key": "agent:main:telegram:direct:670530854",
            "sessionId": "1410a53b", "abortedLastRun": False,
            "tokensFresh": True, "ageMs": 60000,
        }]
    }

    suggestions = build_suggestions(result, {"errors": 0})
    new_sugs = [s for s in suggestions if "/new" in s]
    assert len(new_sugs) == 1
    assert "1410a53b" in new_sugs[0]


def test_suggestion_context_window_compact_at_60pct():
    """A direct session at 60-79% context usage must trigger a /compact
    suggestion (still room, but trim before getting close to the limit)."""
    from openclaw_audit import analyze, build_suggestions

    result = analyze([])
    result["sqlite_info"] = {}
    result["sessions_info"] = {
        "active": [{
            "kind": "direct", "model": "agnes-flash",
            "totalTokens": 170000, "contextTokens": 262144,
            "usagePct": 64.8, "hasTokens": True, "isFailed": False,
            "key": "agent:main:telegram:direct:670530854",
            "sessionId": "1410a53b", "abortedLastRun": False,
            "tokensFresh": True, "ageMs": 60000,
        }]
    }

    suggestions = build_suggestions(result, {"errors": 0})
    compact_sugs = [s for s in suggestions if "/compact" in s]
    assert len(compact_sugs) == 1
    assert "1410a53b" in compact_sugs[0]


def test_suggestion_no_context_suggestion_for_subagent():
    """Subagent sessions (kind=spawn-child) must NOT trigger /compact or
    /new suggestions — they're short-lived and reclaimed by the parent."""
    from openclaw_audit import analyze, build_suggestions

    result = analyze([])
    result["sqlite_info"] = {}
    result["sessions_info"] = {
        "active": [{
            "kind": "spawn-child", "model": "gpt-5.4-mini",
            "totalTokens": 250000, "contextTokens": 258000,
            "usagePct": 96.9, "hasTokens": True, "isFailed": False,
            "key": "agent:main:subagent:abc",
            "sessionId": "abc", "abortedLastRun": False,
            "tokensFresh": True, "ageMs": 1000,
        }]
    }

    suggestions = build_suggestions(result, {"errors": 0})
    ctx_sugs = [s for s in suggestions if "/compact" in s or "/new" in s]
    assert len(ctx_sugs) == 0


def test_suggestion_aborted_main_session_no_tokens():
    """A direct session with abortedLastRun=True and no token data must
    surface an aborted-session suggestion (no context % to compute)."""
    from openclaw_audit import analyze, build_suggestions

    result = analyze([])
    result["sqlite_info"] = {}
    result["sessions_info"] = {
        "active": [{
            "kind": "direct", "model": "agnes-flash",
            "totalTokens": None, "contextTokens": 262144,
            "usagePct": None, "hasTokens": False, "isFailed": False,
            "key": "agent:main:telegram:direct:670530854",
            "sessionId": "1410a53b", "abortedLastRun": True,
            "tokensFresh": False, "ageMs": 240000,
        }]
    }

    suggestions = build_suggestions(result, {"errors": 0})
    abort_sugs = [s for s in suggestions if "abort" in s.lower()]
    assert len(abort_sugs) == 1
    assert "1410a53b" in abort_sugs[0]


def test_session_status_badge_aborted():
    """_session_status_badge must return '⚠ aborted' when abortedLastRun."""
    from openclaw_audit.render import _session_status_badge
    assert _session_status_badge({"abortedLastRun": True}) == "⚠ aborted"


def test_session_status_badge_stale_tokens():
    """_session_status_badge must return '⊘ stale' when tokensFresh=False."""
    from openclaw_audit.render import _session_status_badge
    assert _session_status_badge({"tokensFresh": False}) == "⊘ stale"


def test_session_status_badge_idle_direct():
    """A direct session idle >1h must show '💤 idle'."""
    from openclaw_audit.render import _session_status_badge
    assert _session_status_badge({"kind": "direct", "ageMs": 3700 * 1000}) == "💤 idle"


def test_session_status_badge_active_direct():
    """A direct session ageMs < 1min must show '⚙ active'."""
    from openclaw_audit.render import _session_status_badge
    assert _session_status_badge({"kind": "direct", "ageMs": 30 * 1000}) == "⚙ active"


def test_session_status_badge_empty_for_subagent_idle():
    """Subagent sessions don't get idle/active badges — they're short-lived."""
    from openclaw_audit.render import _session_status_badge
    assert _session_status_badge({"kind": "spawn-child", "ageMs": 3700 * 1000}) == ""


# ─── 1c: long-running / announce give-up / delivery mismatch classifiers ─


def test_classify_long_running_session():
    """openclaw emits `long-running session:` every ~5 min for sessions
    stuck in state=processing. The classifier must tag it so the dashboard
    surfaces it (previously fell into 'other' and was dropped)."""
    from openclaw_audit import classify_entry
    msg = (
        "long-running session: sessionId=15baa03a-a4be-419f-896a-9367e85fb5c9 "
        "sessionKey=agent:main:telegram:direct:670530854 state=processing "
        "age=1185s queueDepth=2 reason=queued_behind_active_work "
        "classification=long_running activeWorkKind=model_call "
        "lastProgress=model_call:started lastProgressAge=7s recovery=none"
    )
    cat = classify_entry(msg, "WARN")
    assert cat["type"] == "long_running_session"
    assert cat["state"] == "processing"
    assert cat["age"] == "1185s"
    assert cat["queueDepth"] == "2"
    assert cat["activeWorkKind"] == "model_call"
    assert cat["lastProgressAge"] == "7s"
    assert cat["sessionKey"] == "agent:main:telegram:direct:670530854"


def test_classify_announce_giveup():
    """Subagent announce give-up (retry-limit hit) must classify as
    announce_giveup so it surfaces as an ERROR event."""
    from openclaw_audit import classify_entry
    msg = (
        "Subagent announce give up (retry-limit) run=7d3e05bb "
        "child=agent:main:subagent:d7f20755 "
        "requester=agent:main:telegram:direct:670530854 retries=3 "
        "endedAgo=18s deliveryError=\"completion agent did not use the message tool\""
    )
    cat = classify_entry(msg, "WARN")
    assert cat["type"] == "announce_giveup"
    assert cat["run"] == "7d3e05bb"
    assert cat["retries"] == "3"
    assert "message tool" in cat.get("deliveryError", "")


def test_classify_delivery_mode_mismatch():
    """source_reply_delivery_mode_mismatch must classify distinctly so the
    dashboard can pair it with announce_giveup for the wedged-parent story."""
    from openclaw_audit import classify_entry
    msg = (
        "Active requester session could not be woken for subagent completion; "
        "falling back to requester-agent handoff: queue_message_failed "
        "reason=source_reply_delivery_mode_mismatch "
        "sessionId=15baa03a-a4be-419f-896a-9367e85fb5c9 gatewayHealth=live"
    )
    cat = classify_entry(msg, "WARN")
    assert cat["type"] == "delivery_mode_mismatch"
    assert cat["sessionId"] == "15baa03a-a4be-419f-896a-9367e85fb5c9"


def test_analyze_long_running_session_surfaces_as_warn_event():
    """A long-running-session log entry must produce a WARN raw_event so
    the dashboard event list shows it (previously it was dropped)."""
    from openclaw_audit import analyze
    msg = (
        "long-running session: sessionId=15baa03a sessionKey=agent:main:telegram:direct:670530854 "
        "state=processing age=585s queueDepth=2 reason=queued_behind_active_work "
        "classification=long_running activeWorkKind=model_call "
        "lastProgress=model_call:stream_progress lastProgressAge=2s recovery=none"
    )
    result = analyze([("openclaw", "2026-06-27T13:21:01+08:00", "WARN", msg)])
    events = result["raw_events"]
    assert len(events) == 1
    assert events[0]["type"] == "⏳ 长时间运行"
    assert events[0]["level"] == "WARN"
    assert "state=processing" in events[0]["detail"]
    assert "age=585s" in events[0]["detail"]
    assert "queueDepth=2" in events[0]["detail"]
    assert "activeWorkKind=model_call" in events[0]["detail"]


def test_analyze_announce_giveup_surfaces_as_error_event():
    """Announce give-up must surface as an ERROR event (it wedges the parent)."""
    from openclaw_audit import analyze
    msg = (
        "Subagent announce give up (retry-limit) run=7d3e05bb "
        "child=agent:main:subagent:d7f20755 "
        "requester=agent:main:telegram:direct:670530854 retries=3 "
        "deliveryError=\"completion agent did not use the message tool\""
    )
    result = analyze([("openclaw", "2026-06-27T08:57:21+08:00", "WARN", msg)])
    events = result["raw_events"]
    assert len(events) == 1
    assert events[0]["type"] == "🔁 Announce放弃"
    assert events[0]["level"] == "ERROR"
    assert "7d3e05bb" in events[0]["detail"]


def test_analyze_delivery_mode_mismatch_surfaces_as_error_event():
    """delivery_mode_mismatch must surface as ERROR so the wedged-parent
    story is visible in the dashboard even when announce_giveup is absent."""
    from openclaw_audit import analyze
    msg = (
        "Active requester session could not be woken for subagent completion; "
        "queue_message_failed reason=source_reply_delivery_mode_mismatch "
        "sessionId=15baa03a gatewayHealth=live"
    )
    result = analyze([("openclaw", "2026-06-27T13:35:12+08:00", "WARN", msg)])
    events = result["raw_events"]
    assert len(events) == 1
    assert events[0]["type"] == "🔗 Delivery状态错配"
    assert events[0]["level"] == "ERROR"
    assert "15baa03a" in events[0]["detail"]
