"""Core analysis: aggregate parsed entries into the audit result dict."""

from collections import Counter, defaultdict

from .classify import classify_entry, classify_litellm_entry
from .util import _truncate
from .parsing import parse_litellm_out_log
from .util import fmt_duration, parse_ts


# ─── 核心分析 ──────────────────────────────────────────────────────
NON_FATAL_LITELLM_TYPES = {"deprecation_warning", "general_error", "other"}

# Window for grouping reply_session_init_conflict events into a single
# "stale lock" burst. The observed 2026-07-09 incident produced 5 conflicts
# in ~77 seconds; a 10-minute window is generous enough to capture slow
# bursts but tight enough that unrelated conflicts on the same key (hours
# apart) do not get merged.
STALE_LOCK_WINDOW_SEC = 600

# Minimum conflict count within the window to qualify as a burst. 2 is
# the lowest non-noise threshold — a single conflict can be a transient
# race; ≥2 on the same sessionKey in 10 min is the observed failure mode.
STALE_LOCK_MIN_CONFLICTS = 2


def _detect_stale_lock_bursts(conflicts_by_key, codex_read_ts, raw_events):
    """Detect reply_session_stale_lock bursts and append composite events.

    Returns the burst count. A burst is ≥ STALE_LOCK_MIN_CONFLICTS
    reply_session_init_conflict events on the same sessionKey within
    STALE_LOCK_WINDOW_SEC, co-occurring with ≥1 codex_history_read_failed
    WARN within STALE_LOCK_WINDOW_SEC of the burst (forward or backward —
    the codex WARN is typically a leading signal logged when the
    embedded run starts and reads a stale mirrored history, so it
    precedes the first conflict by up to a few minutes).

    The composite event is appended to ``raw_events`` and uses the last
    conflict's original ISO ts_str as its ``time`` field so the final
    time-descending sort places it next to its component conflict events
    (NOT at the top of the list by append order — that used to put an
    08:50 composite above a 09:38 event, which broke newest-first
    ordering in the rendered event list).
    """
    bursts = 0
    codex_sorted = sorted(codex_read_ts)
    for session_key, conflicts in conflicts_by_key.items():
        if len(conflicts) < STALE_LOCK_MIN_CONFLICTS:
            continue
        # Sliding window: sort conflicts by time, then group events
        # within STALE_LOCK_WINDOW_SEC of the first event in the burst.
        conflicts_sorted = sorted(conflicts, key=lambda x: x[0])
        i = 0
        n = len(conflicts_sorted)
        while i < n:
            window_start = conflicts_sorted[i][0]
            # Collect all conflicts within STALE_LOCK_WINDOW_SEC of window_start.
            j = i
            while j < n and (conflicts_sorted[j][0] - window_start).total_seconds() <= STALE_LOCK_WINDOW_SEC:
                j += 1
            burst = conflicts_sorted[i:j]
            i = j
            if len(burst) < STALE_LOCK_MIN_CONFLICTS:
                continue
            burst_end = burst[-1][0]
            # Look for a codex_read_ts within STALE_LOCK_WINDOW_SEC of
            # [window_start, burst_end] (lookback + lookahead). The codex
            # WARN is the leading indicator and typically precedes the
            # first conflict by 1-2 min, so a lookback is required.
            has_codex_read = any(
                -STALE_LOCK_WINDOW_SEC <= (cts - window_start).total_seconds() <= STALE_LOCK_WINDOW_SEC
                or -STALE_LOCK_WINDOW_SEC <= (cts - burst_end).total_seconds() <= STALE_LOCK_WINDOW_SEC
                for cts in codex_sorted
            )
            if not has_codex_read:
                continue
            # Burst detected. Use the last conflict's original ts_str so
            # the final time-descending sort lands the composite next to
            # its component conflict events, not at the top of the list
            # by append order.
            bursts += 1
            last_ts_str = burst[-1][2]
            raw_events.append({
                "source": "openclaw", "time": last_ts_str,
                "type": "🔓 Reply session卡死",
                "detail": (
                    f"sessionKey={session_key} 同窗口 ≥2 次 reply session "
                    f"initialization conflicted + codex harness history 读取失败 "
                    f"→ reply session 锁绑定的 sessionId 已被 compaction 轮转,"
                    f"但 lock 没释放 (重启才能恢复)"
                ),
                "level": "ERROR"
            })
    return bursts


def analyze(entries, since=None):
    result = {
        "summary": {},
        "telegram": {"inbound": 0, "outbound": 0, "errors": 0, "send_ok": 0},
        "llm": {"errors": 0, "aborts": 0, "timeouts": 0, "latencies": []},
        "context": {"overflows": 0, "compactions": {"success": 0, "incomplete": 0, "failed": 0}},
        "stalls": 0,
        "transcript_mirror_failures": 0,
        "takeover_silent_gaps": 0,
        "reply_session_init_conflicts": 0,
        "codex_history_read_failures": 0,
        "reply_session_stale_locks": 0,
        "failovers": 0, "connection_issues": 0, "config_reloads": 0,
        "tool_errors": {"edit": 0, "read": 0},
        "incomplete_turns": 0, "other_errors": [],
        "time_series": Counter(),
        "raw_events": [],
        "litellm": {
            "total_requests": 0, "streaming_responses": 0,
            "status_codes": {},
            "upstream_timeouts": 0, "upstream_errors": 0,
            "upstream_connections": 0,
            "fallback_failures": 0,
            "proxy_exceptions": 0,
            "auth_errors": 0,
            "general_errors": 0,
            "warnings": 0,
        },
    }

    hourly_counts = Counter()
    litellm_err_counts = Counter()

    # Per-sessionKey conflict timestamps (parsed_ts, sessionId, original
    # ts_str) for the stale-lock composite post-pass. sessionId may be
    # "unknown" — the composite still works via time-window coincidence
    # with codex_history_read_failed. The original ts_str is preserved so
    # the composite event can use the same sortable ISO format as real
    # log events (the post-pass sorts raw_events by time descending, not
    # by append order, so the format must match for the sort to land the
    # composite next to its component conflict events).
    conflicts_by_session_key = defaultdict(list)
    # Timestamps of codex_history_read_failed WARN events (no sessionKey
    # in the bare message — window-only match).
    codex_history_read_ts = []

    for source, ts_str, level, msg in entries:
        parsed_ts = parse_ts(ts_str)

        if "heartbeat" in msg.lower() or "health-monitor" in msg.lower():
            continue

        if parsed_ts:
            hour_key = parsed_ts.strftime("%H:00")
            hourly_counts[hour_key] += 1

        # ── OpenClaw events ──
        if source == "openclaw":
            cat = classify_entry(msg, level)
            etype = cat.get("type", "other")
            tag_emoji = "📩"
            severity = level

            if etype == "telegram_in":
                result["telegram"]["inbound"] += 1
                tag_emoji = "📩"
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "📩 Telegram消息",
                    "detail": _truncate(msg, 400), "level": level
                })

            elif etype == "telegram_out":
                result["telegram"]["outbound"] += 1
                if cat.get("error"):
                    result["telegram"]["errors"] += 1

            elif etype == "telegram_send_ok":
                # Send succeeded — count separately, NOT into outbound (see
                # classify_entry note about double-counting with "message processed").
                result["telegram"]["send_ok"] += 1
                mid = cat.get("message_id")
                detail = f"messageId={mid}" if mid else _truncate(msg, 400)
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "📤 Telegram回复成功",
                    "detail": detail, "level": "INFO"
                })

            elif etype == "transcript_mirror_failed":
                result["transcript_mirror_failures"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str,
                    "type": "📝 会话记录缺失",
                    "detail": "消息已送达 Telegram,但 session transcript 镜像失败 (session file changed mid-turn)",
                    "level": "WARN"
                })

            elif etype == "takeover_error_silent_gap":
                result["takeover_silent_gaps"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str,
                    "type": "🔇 静默会话记录缺失",
                    "detail": "EmbeddedAttemptSessionTakeoverError 在 lane task / cleanup 路径抛出,mirror 未被调用,transcript 缺失但无 failed to mirror WARN",
                    "level": "ERROR"
                })

            elif etype == "reply_session_init_conflict":
                # Count toward telegram["errors"] so the existing "Telegram
                # 回复失败" suggestion still fires for the dispatch failure,
                # but also track per-sessionKey for the stale-lock composite.
                result["telegram"]["errors"] += 1
                result["reply_session_init_conflicts"] += 1
                sk = cat.get("sessionKey", "")
                if sk and parsed_ts:
                    conflicts_by_session_key[sk].append(
                        (parsed_ts, cat.get("sessionId", ""), ts_str)
                    )
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str,
                    "type": "🔒 Reply session冲突",
                    "detail": _truncate(msg, 400),
                    "level": "ERROR"
                })

            elif etype == "codex_history_read_failed":
                # WARN-level signal: the embedded run's view of the session
                # file is stale. Tracked for the stale-lock composite; also
                # surfaced as a WARN event so the operator sees the leading
                # indicator even when the composite has not yet fired.
                result["codex_history_read_failures"] += 1
                if parsed_ts:
                    codex_history_read_ts.append(parsed_ts)
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str,
                    "type": "📜 Codex历史读取失败",
                    "detail": "embedded run 读取 mirrored session history 失败 (session file 可能已被 compaction 轮转)",
                    "level": "WARN"
                })

            elif etype == "llm_error":
                result["llm"]["errors"] += 1
                if cat.get("reason") == "abort":
                    result["llm"]["aborts"] += 1
                elapsed = cat.get("elapsed_ms", 0)
                if elapsed:
                    result["llm"]["latencies"].append(elapsed / 1000.0)
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "❌ LLM错误",
                    "detail": f"provider={cat.get('provider','?')} elapsed={fmt_duration(elapsed/1000 if elapsed else None)} reason={cat.get('reason','?')}",
                    "level": "ERROR"
                })

            elif etype == "stalled_session":
                result["stalls"] += 1
                detail_parts = [
                    f"reason={cat.get('reason','?')}",
                    f"state={cat.get('state','?')}",
                    f"age={cat.get('age','?')}",
                    f"lastProgressAge={cat.get('lastProgressAge','?')}",
                    f"queueDepth={cat.get('queueDepth','?')}",
                ]
                if cat.get("classification"):
                    detail_parts.append(f"classification={cat['classification']}")
                if cat.get("activeWorkKind"):
                    detail_parts.append(f"activeWorkKind={cat['activeWorkKind']}")
                if cat.get("tool"):
                    detail_parts.append(f"tool={cat['tool']}")
                elif cat.get("lastProgress"):
                    detail_parts.append(f"lastProgress={cat['lastProgress']}")
                if cat.get("recovery"):
                    detail_parts.append(f"recovery={cat['recovery']}")
                if cat.get("sessionKey"):
                    detail_parts.append(f"sessionKey={cat['sessionKey']}")
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "⏸ 卡住会话",
                    "detail": " ".join(detail_parts),
                    "level": "ERROR"
                })

            elif etype == "llm_timeout":
                result["llm"]["timeouts"] += 1
                dur = cat.get("duration_ms", 0) / 1000.0
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "⏰ LLM超时",
                    "detail": f"duration={fmt_duration(dur)}", "level": "ERROR"
                })

            elif etype == "context_overflow":
                result["context"]["overflows"] += 1
                _oc_msgs = cat.get('msg_count')
                _oc_msg_str = _oc_msgs if _oc_msgs is not None else 'n/a'
                _oc_extra = ''
                if cat.get('est_prompt_tokens') is not None:
                    _oc_extra += f" est={cat['est_prompt_tokens']}"
                if cat.get('overflow_tokens') is not None:
                    _oc_extra += f" over={cat['overflow_tokens']}"
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "📦 上下文溢出",
                    "detail": f"subtype={cat.get('subtype','?')} msgs={_oc_msg_str}{_oc_extra}",
                    "level": "WARN"
                })

            elif etype == "compaction":
                sub = cat.get("subtype", "?")
                if sub == "success":
                    result["context"]["compactions"]["success"] += 1
                elif sub in ("incomplete", "start"):
                    result["context"]["compactions"]["incomplete"] += 1

            elif etype == "failover":
                result["failovers"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "🔄 Failover",
                    "detail": _truncate(msg, 400), "level": "WARN"
                })

            elif etype == "incomplete_turn":
                result["incomplete_turns"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "⚠️ 不完整响应",
                    "detail": f"provider={cat.get('provider','?')}", "level": "WARN"
                })

            elif etype == "telegram_conn_issue":
                result["connection_issues"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "🔌 Telegram断连",
                    "detail": _truncate(msg, 400), "level": "WARN"
                })

            elif etype == "config_reload":
                result["config_reloads"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "⚙️ 配置热加载",
                    "detail": _truncate(msg, 400), "level": "INFO"
                })

            elif etype == "edit_failed":
                result["tool_errors"]["edit"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "✏️ Edit失败",
                    "detail": _truncate(msg, 400), "level": "WARN"
                })

            elif etype == "read_failed":
                result["tool_errors"]["read"] += 1

            elif etype == "long_running_session":
                # openclaw emits this every ~5 min for sessions stuck in
                # state=processing. Surface as a WARN event so the dashboard
                # shows the session is taking long, even if it hasn't yet
                # escalated to stalled_session. Don't double-count: if the
                # same session is also classified as stalled_session (which
                # carries more detail), that one wins.
                detail_parts = [
                    f"state={cat.get('state','?')}",
                    f"age={cat.get('age','?')}",
                    f"queueDepth={cat.get('queueDepth','?')}",
                ]
                if cat.get("activeWorkKind"):
                    detail_parts.append(f"activeWorkKind={cat['activeWorkKind']}")
                if cat.get("lastProgressAge"):
                    detail_parts.append(f"lastProgressAge={cat['lastProgressAge']}")
                if cat.get("sessionKey"):
                    detail_parts.append(f"sessionKey={cat['sessionKey']}")
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "⏳ 长时间运行",
                    "detail": " ".join(detail_parts),
                    "level": "WARN"
                })

            elif etype == "announce_giveup":
                # Subagent announce retry-limit hit — parent will be wedged.
                # Surface as ERROR so it ranks high in the event list.
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str,
                    "type": "🔁 Announce放弃",
                    "detail": _truncate(msg, 400),
                    "level": "ERROR"
                })

            elif etype == "delivery_mode_mismatch":
                # Parent session delivery state desynced — pairs with
                # announce_giveup but on a different code path.
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str,
                    "type": "🔗 Delivery状态错配",
                    "detail": _truncate(msg, 400),
                    "level": "ERROR"
                })

            elif etype in ("lane_error", "fetch_timeout", "agent_end"):
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": f"⚠️ {cat.get('type','?')}",
                    "detail": _truncate(msg, 400),
                    "level": "WARN" if etype != "agent_end" else "INFO"
                })

            elif etype == "unknown_error":
                result["other_errors"].append(_truncate(msg, 400))
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": f"❌ {level}",
                    "detail": _truncate(msg, 400), "level": level
                })

        # ── LiteLLM events ──
        elif source == "litellm":
            cat = classify_litellm_entry(msg)
            etype = cat.get("type", "other")

            if etype == "upstream_timeout":
                result["litellm"]["upstream_timeouts"] += 1
                litellm_err_counts["upstream_timeout"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "⏰ Litellm上游超时",
                    "detail": _truncate(msg, 400), "level": "ERROR"
                })

            elif etype == "upstream_connection":
                result["litellm"]["upstream_errors"] += 1
                result["litellm"]["upstream_connections"] += 1
                litellm_err_counts["upstream_connection"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "🔌 Litellm连接错误",
                    "detail": _truncate(msg, 400), "level": "ERROR"
                })

            elif etype == "upstream_fallback_failed":
                result["litellm"]["upstream_errors"] += 1
                result["litellm"]["fallback_failures"] += 1
                litellm_err_counts["fallback_failed"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "❌ Litellm Fallback失败",
                    "detail": _truncate(msg, 400), "level": "ERROR"
                })

            elif etype == "auth_error":
                result["litellm"]["auth_errors"] += 1
                litellm_err_counts["auth_error"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "🔑 Litellm鉴权失败",
                    "detail": _truncate(msg, 400), "level": "ERROR"
                })

            elif etype == "proxy_exception":
                result["litellm"]["upstream_errors"] += 1
                result["litellm"]["proxy_exceptions"] += 1
                litellm_err_counts["proxy_exception"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "❌ Litellm代理异常",
                    "detail": _truncate(msg, 400), "level": "ERROR"
                })

            elif etype == "deprecation_warning":
                result["litellm"]["warnings"] += 1

            elif etype == "general_error":
                result["litellm"]["upstream_errors"] += 1
                result["litellm"]["general_errors"] += 1

    # ── Also read litellm.out log for request stats ──
    litellm_out = parse_litellm_out_log(since)
    result["litellm"]["total_requests"] = litellm_out["total_requests"]
    result["litellm"]["streaming_responses"] = litellm_out["streaming_responses"]
    result["litellm"]["status_codes"] = dict(litellm_out["status_codes"])

    # ── Composite: reply_session_stale_lock ──
    # A burst of ≥2 reply_session_init_conflict events on the same
    # sessionKey within STALE_LOCK_WINDOW_SEC, plus ≥1 codex_history_read_failed
    # WARN in the same window, indicates the reply session lock is wedged
    # on a stale sessionId after compaction rotated the underlying file.
    # The Telegram send chain itself is fine (the previous turn usually
    # delivered); the failure is in session-state bookkeeping. Without
    # this composite the operator sees "Telegram 回复失败" (misleading —
    # the send chain is healthy) and a bare "Codex历史读取失败" WARN,
    # with no link between them.
    if conflicts_by_session_key and codex_history_read_ts:
        result["reply_session_stale_locks"] = _detect_stale_lock_bursts(
            conflicts_by_session_key,
            codex_history_read_ts,
            result["raw_events"],
        )

    # ── Summary ──
    total_events = len(entries)
    result["summary"] = {
        "total_events": total_events,
        "telegram_in": result["telegram"]["inbound"],
        "telegram_out": result["telegram"]["outbound"],
        "telegram_send_ok": result["telegram"]["send_ok"],
        "llm_errors": result["llm"]["errors"],
        "llm_aborts": result["llm"]["aborts"],
        "llm_timeouts": result["llm"]["timeouts"],
        "session_stalls": result["stalls"],
        "failovers": result["failovers"],
        "context_overflows": result["context"]["overflows"],
        "compaction_success": result["context"]["compactions"]["success"],
        "incomplete_turns": result["incomplete_turns"],
        "connection_issues": result["connection_issues"],
        "transcript_mirror_failures": result["transcript_mirror_failures"],
        "takeover_silent_gaps": result["takeover_silent_gaps"],
        "reply_session_init_conflicts": result["reply_session_init_conflicts"],
        "codex_history_read_failures": result["codex_history_read_failures"],
        "reply_session_stale_locks": result["reply_session_stale_locks"],
        "config_reloads": result["config_reloads"],
        "edit_fails": result["tool_errors"]["edit"],
        "avg_llm_latency": None,
        "litellm_requests": result["litellm"]["total_requests"],
        "litellm_upstream_timeouts": result["litellm"]["upstream_timeouts"],
        "litellm_upstream_errors": result["litellm"]["upstream_errors"],
        "litellm_upstream_connections": result["litellm"]["upstream_connections"],
        "litellm_fallback_failures": result["litellm"]["fallback_failures"],
        "litellm_proxy_exceptions": result["litellm"]["proxy_exceptions"],
        "litellm_auth_errors": result["litellm"]["auth_errors"],
        "litellm_general_errors": result["litellm"]["general_errors"],
    }

    if result["llm"]["latencies"]:
        lats = sorted(result["llm"]["latencies"])
        result["summary"]["avg_llm_latency"] = sum(lats) / len(lats)
        result["summary"]["max_llm_latency"] = max(lats)
        idx95 = int(len(lats) * 0.95)
        result["summary"]["p95_llm_latency"] = lats[min(idx95, len(lats)-1)]

    result["time_series"] = dict(sorted(hourly_counts.items()))
    # Sort raw_events by time descending (newest first). The previous
    # `reverse()` assumed raw_events were appended in chronological order,
    # but the reply_session_stale_lock composite is appended AFTER the
    # main loop with a ts that may be older than later-processed events —
    # reverse() would put it at the top regardless of its real timestamp.
    # ISO 8601 strings sort lexically = chronologically (all events share
    # the same tz offset from one host, so the offset never breaks the
    # lexical order). Stable sort preserves insertion order for events
    # at the same second, keeping the composite just after its component
    # conflict events.
    result["raw_events"].sort(key=lambda ev: ev.get("time", ""), reverse=True)

    return result
