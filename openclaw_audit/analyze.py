"""Core analysis: aggregate parsed entries into the audit result dict."""

from collections import Counter

from .classify import classify_entry, classify_litellm_entry
from .parsing import parse_litellm_out_log
from .util import fmt_duration, parse_ts


# ─── 核心分析 ──────────────────────────────────────────────────────
NON_FATAL_LITELLM_TYPES = {"deprecation_warning", "general_error", "other"}

def analyze(entries, since=None):
    result = {
        "summary": {},
        "telegram": {"inbound": 0, "outbound": 0, "errors": 0, "send_ok": 0},
        "llm": {"errors": 0, "aborts": 0, "timeouts": 0, "latencies": []},
        "context": {"overflows": 0, "compactions": {"success": 0, "incomplete": 0, "failed": 0}},
        "stalls": 0,
        "transcript_mirror_failures": 0,
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
                    "detail": msg[:200], "level": level
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
                detail = f"messageId={mid}" if mid else msg[:200]
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
                    f"classification={cat.get('classification','?')}",
                    f"state={cat.get('state','?')}",
                    f"age={cat.get('age','?')}",
                    f"lastProgressAge={cat.get('lastProgressAge','?')}",
                    f"queueDepth={cat.get('queueDepth','?')}",
                ]
                if cat.get("activeWorkKind"):
                    detail_parts.append(f"activeWorkKind={cat['activeWorkKind']}")
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
                    "detail": msg[:200], "level": "WARN"
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
                    "detail": msg[:200], "level": "WARN"
                })

            elif etype == "config_reload":
                result["config_reloads"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "⚙️ 配置热加载",
                    "detail": msg[:200], "level": "INFO"
                })

            elif etype == "edit_failed":
                result["tool_errors"]["edit"] += 1
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": "✏️ Edit失败",
                    "detail": msg[:200], "level": "WARN"
                })

            elif etype == "read_failed":
                result["tool_errors"]["read"] += 1

            elif etype in ("lane_error", "fetch_timeout", "agent_end"):
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": f"⚠️ {cat.get('type','?')}",
                    "detail": msg[:200],
                    "level": "WARN" if etype != "agent_end" else "INFO"
                })

            elif etype == "unknown_error":
                result["other_errors"].append(msg[:200])
                result["raw_events"].append({
                    "source": "openclaw", "time": ts_str, "type": f"❌ {level}",
                    "detail": msg[:200], "level": level
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
                    "detail": msg[:200], "level": "ERROR"
                })

            elif etype == "upstream_connection":
                result["litellm"]["upstream_errors"] += 1
                result["litellm"]["upstream_connections"] += 1
                litellm_err_counts["upstream_connection"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "🔌 Litellm连接错误",
                    "detail": msg[:200], "level": "ERROR"
                })

            elif etype == "upstream_fallback_failed":
                result["litellm"]["upstream_errors"] += 1
                result["litellm"]["fallback_failures"] += 1
                litellm_err_counts["fallback_failed"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "❌ Litellm Fallback失败",
                    "detail": msg[:200], "level": "ERROR"
                })

            elif etype == "auth_error":
                result["litellm"]["auth_errors"] += 1
                litellm_err_counts["auth_error"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "🔑 Litellm鉴权失败",
                    "detail": msg[:200], "level": "ERROR"
                })

            elif etype == "proxy_exception":
                result["litellm"]["upstream_errors"] += 1
                result["litellm"]["proxy_exceptions"] += 1
                litellm_err_counts["proxy_exception"] += 1
                result["raw_events"].append({
                    "source": "litellm", "time": ts_str, "type": "❌ Litellm代理异常",
                    "detail": msg[:200], "level": "ERROR"
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
    result["raw_events"].reverse()

    return result
