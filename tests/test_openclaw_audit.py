import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "openclaw-audit.py"
SPEC = importlib.util.spec_from_file_location("openclaw_audit", MODULE_PATH)
openclaw_audit = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(openclaw_audit)

INSIGHTS_PATH = Path(__file__).resolve().parents[1] / "openclaw_audit_insights.py"
INSIGHTS_SPEC = importlib.util.spec_from_file_location("openclaw_audit_insights", INSIGHTS_PATH)
openclaw_audit_insights = importlib.util.module_from_spec(INSIGHTS_SPEC)
assert INSIGHTS_SPEC and INSIGHTS_SPEC.loader
INSIGHTS_SPEC.loader.exec_module(openclaw_audit_insights)


def test_classify_entry_detects_stalled_session():
    msg = (
        "stalled session: sessionId=bbb0cc71-f793-4856-bedc-0e64fe71236b "
        "sessionKey=agent:main:telegram:direct:670530854 state=processing "
        "age=122s queueDepth=1 reason=active_work_without_progress "
        "classification=stalled_agent_run activeWorkKind=model_call "
        "lastProgress=model_call:started lastProgressAge=122s recovery=none"
    )

    cat = openclaw_audit.classify_entry(msg, "WARN")

    assert cat["type"] == "stalled_session"
    assert cat["reason"] == "active_work_without_progress"
    assert cat["classification"] == "stalled_agent_run"
    assert cat["state"] == "processing"
    assert cat["lastProgressAge"] == "122s"


def test_classify_entry_marks_llm_abort():
    msg = "model-fetch error provider=litellm elapsedMs=17600 AbortError: request aborted"

    cat = openclaw_audit.classify_entry(msg, "ERROR")

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

    result = openclaw_audit.analyze(entries)
    suggestions = openclaw_audit.build_suggestions(result, {"errors": 0})

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

    summary = openclaw_audit_insights.build_root_cause_summary(result)

    assert "执行层卡住" in summary
