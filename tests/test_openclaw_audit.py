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
