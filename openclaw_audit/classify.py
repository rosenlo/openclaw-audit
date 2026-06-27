"""Event classification: turn raw log messages into typed category dicts."""

from .util import _extract_fields, _parse_int_field


# ─── LiteLLM 错误分类 ─────────────────────────────────────────────
def classify_litellm_entry(msg):
    ml = msg.lower()
    cat = {"type": "other"}

    # ReadTimeout / APIConnectionError (from agnes upstream)
    if "readtimeout" in ml or "timeout on reading" in ml:
        cat["type"] = "upstream_timeout"
        return cat
    if "apiconnectionerror" in ml or "midstreamfallback" in ml:
        cat["type"] = "upstream_connection"
        return cat
    if "fallback also failed" in ml:
        cat["type"] = "upstream_fallback_failed"
        return cat
    # Auth failures carry "Exception occured" too (auth_exception_handler /
    # user_api_key_auth / "No api key passed"), so detect them BEFORE the
    # generic proxy_exception bucket or they get mislabeled as a runtime
    # proxy error and mislead triage.
    if (
        "no api key passed" in ml
        or "auth_exception_handler" in ml
        or "user_api_key_auth" in ml
    ):
        cat["type"] = "auth_error"
        return cat
    if "exception occured" in ml:
        cat["type"] = "proxy_exception"
        return cat

    # set_verbose deprecation warning
    if "set_verbose is deprecated" in ml:
        cat["type"] = "deprecation_warning"
        return cat

    # General error
    if ":error" in ml or "traceback" in ml:
        cat["type"] = "general_error"
        return cat

    return cat


# ─── OpenClaw 事件分类 ─────────────────────────────────────────────
def classify_entry(msg, level):
    ml = msg.lower()
    cat = {}

    if "inbound message" in ml and "telegram" in ml:
        cat["type"] = "telegram_in"
        try:
            chars_part = msg.rsplit(", ", 1)[-1] if ", " in msg else ""
            cat["chars"] = int(chars_part.replace(" chars)", "").replace(" chars", ""))
        except (ValueError, IndexError):
            cat["chars"] = 0
        return cat

    if "message processed" in ml and "telegram" in ml:
        cat["type"] = "telegram_out"
        if "outcome=error" in ml:
            cat["error"] = True
        for part in msg.split():
            if part.startswith("duration="):
                try:
                    cat["duration_ms"] = int(part.replace("duration=", "").replace("ms", ""))
                except ValueError:
                    pass
        return cat

    # Telegram send succeeded. Two distinct subsystems log this, with
    # different wording and field names:
    #   - telegram/send (queued delivery-queue sends):
    #       "telegram outbound send ok accountId=default chatId=670530854
    #        messageId=1956 operation=sendRichMessage deliveryKind=text ..."
    #   - channels/telegram (direct / non-queued sends):
    #       "telegram sendRichMessage ok chat=670530854 message=1964"
    # These are MUTUALLY EXCLUSIVE send paths (verified 2026-06-21: across
    # 06-20/06-21 logs the two messageId sets are disjoint — no message
    # number appears in both), so counting both into send_ok does NOT
    # double-count. The "message processed" line is a separate diagnostic
    # event (almost always a cron-job timeout error, channel=cron,
    # messageId=unknown), NOT a real reply, and is handled as telegram_out.
    if "telegram outbound send ok" in ml or (
        "sendrichmessage ok" in ml and "telegram" in ml
    ):
        cat["type"] = "telegram_send_ok"
        for part in msg.split():
            if part.startswith("messageId="):
                cat["message_id"] = part.split("=", 1)[1]
            elif part.startswith("message="):
                cat["message_id"] = part.split("=", 1)[1]
            elif part.startswith("chatId="):
                cat["chat_id"] = part.split("=", 1)[1]
            elif part.startswith("chat="):
                cat["chat_id"] = part.split("=", 1)[1]
        return cat

    # Outbound delivery was mirrored into the session transcript, but the
    # session file had changed underneath the deliverer (typically because
    # a compaction rotated the transcript mid-turn). The channel send to
    # Telegram already succeeded, so the user saw the reply — but the
    # session transcript is now missing that delivery, which can break
    # later compaction/context replay. Surfaced as its own category so it
    # is not buried under generic WARN noise.
    if "failed to mirror" in ml and "session transcript" in ml:
        cat["type"] = "transcript_mirror_failed"
        return cat

    # Silent transcript gap variant: when EmbeddedAttemptSessionTakeoverError
    # fires on the lane task / cleanup path (e.g. prompt reacquire after a
    # compaction), the mirror call is never reached, so the
    # "failed to mirror" WARN above is NOT logged — the gap is silent.
    # This typically surfaces as `lane task error: ... error="EmbeddedAttemptSessionTakeoverError: ..."`
    # or `Embedded agent failed before reply: ...`. Catch it here so the gap
    # stays observable instead of disappearing into generic ERROR noise.
    if "embeddedattemptsessiontakeovererror" in ml:
        cat["type"] = "takeover_error_silent_gap"
        return cat

    if "model-fetch" in ml and "error" in ml:
        cat["type"] = "llm_error"
        for part in msg.split():
            if part.startswith("elapsedMs="):
                try:
                    cat["elapsed_ms"] = int(part.split("=")[1])
                except (ValueError, IndexError):
                    pass
            if part.startswith("provider="):
                cat["provider"] = part.split("=")[1]
        cat["reason"] = "abort" if ("AbortError" in msg or "abort" in ml) else "error"
        return cat

    if "stalled session" in ml or "active_work_without_progress" in ml or "stalled_agent_run" in ml:
        cat["type"] = "stalled_session"
        cat.update(
            _extract_fields(
                msg,
                [
                    "sessionId",
                    "sessionKey",
                    "state",
                    "age",
                    "queueDepth",
                    "reason",
                    "classification",
                    "activeWorkKind",
                    "lastProgress",
                    "lastProgressAge",
                    "recovery",
                    "toolName",
                ],
            )
        )
        # Derive a short tool name when the stall is on a tool_call. For
        # model_call, OpenClaw emits lastProgress as 'model_call:started'
        # (kind:state, no name to surface). For tool_call it carries the
        # tool name, e.g. 'tool_call:Bash:started' or 'tool_call:Bash'.
        # Surface just the name so the audit detail points at the
        # offending tool without the operator having to read the raw
        # progress string. If the segment is a bare state word (no name
        # emitted), leave `tool` unset and let the display fall back to
        # showing lastProgress verbatim.
        lp = cat.get("lastProgress", "")
        if lp.startswith("tool_call:"):
            rest = lp[len("tool_call:"):]
            head = rest.split(":", 1)[0] if ":" in rest else rest
            if head and head not in {"started", "completed", "pending", "running"}:
                cat["tool"] = head
        return cat

    if "context overflow" in ml or "context-overflow" in ml:
        cat["type"] = "context_overflow"
        cat["subtype"] = "precheck" if "precheck" in ml else ("diagnostic" if "diag" in ml else "detected")
        for part in msg.split():
            if part.startswith("messages="):
                # OpenClaw emits `messages=NaN` when messageCount is unresolved
                # at the precheck stage; int("NaN") raises, so fall back to None
                # rather than silently dropping the field (which renders as `?`).
                cat["msg_count"] = _parse_int_field(part)
            elif part.startswith("estimatedPromptTokens="):
                cat["est_prompt_tokens"] = _parse_int_field(part)
            elif part.startswith("overflowTokens="):
                cat["overflow_tokens"] = _parse_int_field(part)
        return cat

    if "auto-compaction" in ml:
        cat["type"] = "compaction"
        if "succeeded" in ml:
            cat["subtype"] = "success"
        elif "incomplete" in ml:
            cat["subtype"] = "incomplete"
        elif "start" in ml:
            cat["subtype"] = "start"
        else:
            cat["subtype"] = "retry"
        return cat

    if "failover" in ml:
        cat["type"] = "failover"
        return cat

    if "llm request timed out" in ml or ("timed out" in ml and "llm" in ml):
        cat["type"] = "llm_timeout"
        for part in msg.split():
            if part.startswith("durationMs="):
                try:
                    cat["duration_ms"] = int(part.split("=")[1])
                except (ValueError, IndexError):
                    pass
        return cat

    if "incomplete turn" in ml:
        cat["type"] = "incomplete_turn"
        for part in msg.split():
            if part.startswith("provider="):
                cat["provider"] = part.split("=")[1]
        return cat

    if "edit failed" in ml:
        cat["type"] = "edit_failed"
        return cat

    if "read failed" in ml:
        cat["type"] = "read_failed"
        return cat

    if "config hot reload" in ml:
        cat["type"] = "config_reload"
        return cat

    if "lane task error" in ml:
        cat["type"] = "lane_error"
        for part in msg.split():
            if part.startswith("durationMs="):
                try:
                    cat["duration_ms"] = int(part.split("=")[1])
                except (ValueError, IndexError):
                    pass
        return cat

    if "fetch fallback" in ml or "closed before connect" in ml:
        cat["type"] = "telegram_conn_issue"
        return cat

    if "fetch timeout" in ml:
        cat["type"] = "fetch_timeout"
        return cat

    if "embedded run agent end" in ml:
        cat["type"] = "agent_end"
        return cat

    # long-running session: openclaw emits this every ~5 min for sessions
    # stuck in state=processing. Distinct from stalled_session (which is
    # openclaw's own classification) — long-running is the early warning
    # before stalled_session fires.
    if "long-running session" in ml:
        cat["type"] = "long_running_session"
        cat.update(
            _extract_fields(
                msg,
                [
                    "sessionId",
                    "sessionKey",
                    "state",
                    "age",
                    "queueDepth",
                    "reason",
                    "classification",
                    "activeWorkKind",
                    "lastProgress",
                    "lastProgressAge",
                    "recovery",
                ],
            )
        )
        return cat

    # Subagent announce give-up: the child finished, but the parent
    # never received the result because the retry-limit was hit. This is
    # the "wedged parent" event — the parent will park in state=processing
    # indefinitely. The exact log line is:
    #   "Subagent announce give up (retry-limit) run=... child=...
    #    requester=... retries=N ... deliveryError=..."
    if "announce give up" in ml or "announce give-up" in ml:
        cat["type"] = "announce_giveup"
        cat.update(
            _extract_fields(
                msg,
                ["run", "child", "requester", "retries", "endedAgo", "deliveryError"],
            )
        )
        return cat

    # source_reply_delivery_mode_mismatch: the parent session's delivery
    # state machine desynced from the subagent's completion — the parent
    # will never receive the result. Pairs with announce_giveup but is
    # emitted on a different code path (requester wake failure).
    if "source_reply_delivery_mode_mismatch" in ml:
        cat["type"] = "delivery_mode_mismatch"
        cat.update(_extract_fields(msg, ["sessionId", "gatewayHealth"]))
        return cat

    if level in ("ERROR", "WARN", "FATAL"):
        cat["type"] = "unknown_error"
        cat["level"] = level
        return cat

    cat["type"] = "other"
    return cat
