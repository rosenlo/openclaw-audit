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


def test_litellm_err_log_date_inference_across_midnight():
    """litellm err lines have only HH:MM:SS. When the time wraps backwards
    the parser must advance the inferred date by one day, so a 23:59 -> 00:01
    sequence is stamped on consecutive days (not both on 'today')."""
    import tempfile, os
    from openclaw_audit import parsing

    lines = [
        "23:59:58 - LiteLLM Router:ERROR: late-night error\n",
        "00:01:05 - LiteLLM Router:ERROR: after-midnight error\n",
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

    assert len(entries) == 2
    d1 = entries[0][1].split("T")[0]
    d2 = entries[1][1].split("T")[0]
    # second line is the next calendar day relative to the first
    assert d2 > d1, f"expected date to advance across midnight: {d1} -> {d2}"


def test_litellm_err_log_same_day_no_spurious_advance():
    """A normal increasing-time sequence within one day must NOT advance the
    date — guards against the cursor over-rotating on same-day logs."""
    import tempfile, os
    from openclaw_audit import parsing

    lines = [
        "10:00:00 - LiteLLM Router:ERROR: first\n",
        "10:05:00 - LiteLLM Router:ERROR: second\n",
        "10:10:00 - LiteLLM Router:ERROR: third\n",
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

    dates = {e[1].split("T")[0] for e in entries}
    assert len(dates) == 1, f"all same-day lines should share one date, got {dates}"


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
    parser must use it directly instead of inferring from HH:MM:SS."""
    from openclaw_audit import parsing
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
    assert ts == "2026-06-20T22:25:57+07:00", ts
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


def test_litellm_json_and_text_lines_coexist():
    """A file may contain both JSON lines and legacy text lines (e.g. during
    a config flip). Both must parse, each with its own correct date."""
    from openclaw_audit import parsing
    lines = [
        # legacy text line, same day
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

    assert len(entries) == 2
    # JSON entry keeps its own real date; text entry keeps today's date.
    dates = {e[1].split("T")[0] for e in entries}
    assert "2026-06-20" in dates


def test_litellm_text_same_day_reorder_not_misread_as_midnight():
    """The original bug: text lines 22:25:53 followed by 22:22:24 (a small
    backwards step from same-day reordering) were misread as a midnight
    crossing and stamped on the next day, floating them above later events
    in the sort. With the 12h threshold, the small backstep must NOT advance
    the date."""
    from openclaw_audit import parsing
    lines = [
        "22:25:53 - LiteLLM Router:ERROR: auth_exception_handler.py:97 - "
        "No api key passed in.\n",
        "22:22:24 - LiteLLM Router:ERROR: auth_exception_handler.py:97 - "
        "No api key passed in.\n",
        "22:42:22 - LiteLLM Router:ERROR: auth_exception_handler.py:97 - "
        "No api key passed in.\n",
    ]
    path = _write_tmp_log(lines)
    try:
        orig = parsing.LITELLM_ERR_LOG
        parsing.LITELLM_ERR_LOG = path
        entries = parsing.parse_litellm_err_log(since=None)
    finally:
        parsing.LITELLM_ERR_LOG = orig
        import os; os.unlink(path)

    assert len(entries) == 3
    # All three must share one date — none promoted to "tomorrow".
    dates = {e[1].split("T")[0] for e in entries}
    assert len(dates) == 1, f"same-day reorder should not split dates, got {dates}"


def test_litellm_text_real_midnight_still_advances():
    """A genuine midnight crossing (23:59 -> 00:01) is a large backstep and
    must still advance the date by one day. Guards the threshold against
    being so strict it breaks real cross-midnight logs."""
    from openclaw_audit import parsing
    lines = [
        "23:59:58 - LiteLLM Router:ERROR: late-night error\n",
        "00:01:05 - LiteLLM Router:ERROR: after-midnight error\n",
    ]
    path = _write_tmp_log(lines)
    try:
        orig = parsing.LITELLM_ERR_LOG
        parsing.LITELLM_ERR_LOG = path
        entries = parsing.parse_litellm_err_log(since=None)
    finally:
        parsing.LITELLM_ERR_LOG = orig
        import os; os.unlink(path)

    assert len(entries) == 2
    d1 = entries[0][1].split("T")[0]
    d2 = entries[1][1].split("T")[0]
    assert d2 > d1, f"expected date to advance across midnight: {d1} -> {d2}"


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
