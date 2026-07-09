"""Insight helpers for openclaw-audit.

This module keeps the higher-level interpretation logic out of the
single-file CLI entrypoint so the report/web layers can share one source
of truth for recommendations and root-cause language.
"""


def build_suggestions(result, tg_result=None):
    """Build the suggestion list from the current audit result."""
    s = result["summary"]
    l = result.get("litellm", {})
    tg_result = tg_result or result.get("telegram", {})

    suggestions = []
    if s["session_stalls"] > 0:
        suggestions.append("🟡 任务卡住但未必断连 — 检查 stalled_agent_run / active_work_without_progress")
    if s["llm_aborts"] > 0:
        suggestions.append("🟡 LLM 请求被 abort / failover — 优先看上游链路而不是 Telegram")
    if l.get("auth_errors", 0) > 0:
        suggestions.append("⚪ LiteLLM 鉴权失败 — 通常是本机 /models 探测请求未带 key（No api key passed），不影响聊天链路；若非本机请求再查 API key 配置")
    if l.get("proxy_exceptions", 0) > 0:
        suggestions.append("🔴 LiteLLM 代理异常 — 先查 LitelLM / proxy 日志")
    if l.get("upstream_timeouts", 0) > 0:
        suggestions.append("🔴 LiteLLM 上游超时 — 上游响应慢或超时")
    if s["llm_timeouts"] >= 3:
        suggestions.append("🔴 LLM 频繁超时 — 检查 litellm upstream 响应速度, 或降低 litellm 的 request_timeout 配置")
    if s["litellm_upstream_timeouts"] >= 5:
        suggestions.append("🔴 Litellm 上游(agnes)频繁超时 — 上游 API 响应慢, 建议检查 agnes API 状态或增大 timeout 配置")
    if s["context_overflows"] > 3:
        suggestions.append("🟡 上下文溢出频繁 — 考虑 /compact 或减少单轮 Tool 调用量")
    if s["connection_issues"] > 2:
        suggestions.append("🟡 Telegram连接不稳 — 检查网络/代理, 多数自动恢复")
    if s["edit_fails"] > 5:
        suggestions.append("🟡 Edit工具失败过多 — 长文本编辑建议用 Write 替代 Edit")
    if s.get("transcript_mirror_failures", 0) > 0:
        suggestions.append("🟡 会话记录镜像失败 — 消息已送达 Telegram 但 session transcript 缺失,留意后续 compaction 是否丢上下文")
    if s.get("takeover_silent_gaps", 0) > 0:
        suggestions.append("🔴 静默会话记录缺失 — EmbeddedAttemptSessionTakeoverError 在 mirror 调用前抛出,transcript gap 无 WARN,需结合 lane task error 日志定位")
    if s.get("reply_session_stale_locks", 0) > 0:
        suggestions.append(
            "🔴 Reply session 卡死 (stale lock) — 同窗口 ≥2 次 reply session "
            "initialization conflicted + codex harness history 读取失败,"
            "说明 compaction 轮转后 reply session 仍绑定旧 sessionId,lock 不释放,"
            "重启 gateway 才能恢复。属于 #88838 范围 (等 #96625 SQLite flip 修复),"
            "短期重启即可,勿上报新 PR"
        )
    elif s.get("reply_session_init_conflicts", 0) > 0:
        # Conflict events without the codex_read co-signal — still worth
        # surfacing, but the operator should know the composite was not
        # triggered (might be a different root cause).
        suggestions.append(
            f"🟡 Reply session 初始化冲突 {s['reply_session_init_conflicts']} 次 "
            f"(未触发 stale-lock 组合) — Telegram 上一轮发送通常成功,"
            f"问题在 session 状态而非发送链路"
        )
    if tg_result.get("errors", 0) > 0:
        suggestions.append("🔴 Telegram 回复失败 — 需关注消息发送链路")
    if l.get("warnings", 0) > 0:
        suggestions.append("🟡 Litellm 配置警告 — 将 set_verbose 改为 LITELLM_LOG=DEBUG")
    suggestions.extend(_sqlite_failure_suggestions(result.get("sqlite_info") or {}))
    suggestions.extend(_session_state_suggestions(result.get("sessions_info") or {}))
    if not suggestions:
        suggestions.append("✅ 系统运行正常")
    return suggestions


def _sqlite_failure_suggestions(sqlite_info):
    """Turn recent task_run / subagent announce failures into suggestions.

    These come from query_sqlite() reading the live SQLite state. We only
    surface failures from the last 60 minutes so the suggestion list stays
    focused on the current window, not historical errors.
    """
    out = []
    recents = sqlite_info.get("recent_task_failures") or []
    recent_count = sum(1 for r in recents if (r.get("age_min") or 0) <= 60)
    if recent_count > 0:
        # Pick the most common error text (case-folded) among recent
        # failures so the suggestion points at the dominant cause, not a
        # random one.
        recent = [r for r in recents if (r.get("age_min") or 0) <= 60]
        err_counts = {}
        for r in recent:
            err = r.get("error") or ""
            key = err[:80].lower()
            err_counts[key] = (err_counts.get(key, [0, err])[0] + 1, err)
        top_key = max(err_counts, key=lambda k: err_counts[k][0])
        top_err = err_counts[top_key][1]
        if "no api key" in top_err.lower():
            out.append(
                f"🔴 子Agent启动失败 {recent_count} 次 (近1h) — FailoverError: "
                f"openai provider 缺 API key。检查 `openclaw agents add main` "
                f"或 agent 的 auth store（/Users/rosen/.openclaw/agents/main/agent/openclaw-agent.sqlite）"
            )
        elif "codex subscription usage limit" in top_err.lower():
            reset_match = ""
            for r in recent:
                if "reset" in (r.get("error") or "").lower():
                    reset_match = " — " + r["error"]
                    break
            out.append(
                f"🔴 子Agent失败 {recent_count} 次 (近1h) — Codex 订阅用量到顶"
                f"{reset_match[:120]}"
            )
        else:
            out.append(
                f"🔴 子Agent失败 {recent_count} 次 (近1h) — {top_err[:120]}"
            )
    ann_failures = sqlite_info.get("recent_announce_failures") or []
    ann_recent = [r for r in ann_failures if (r.get("age_min") or 0) <= 60]
    if ann_recent:
        out.append(
            f"🔴 Announce give-up {len(ann_recent)} 次 (近1h) — 子Agent 完成但结果"
            f"未送达主 session (parent 可能 wedged)。最近: "
            f"{ann_recent[0].get('error','')[:100]}"
        )
    return out


def _session_state_suggestions(sessions_info):
    """Suggest /compact or /new based on context window usage and session
    state markers (abortedLastRun, idle age, stale tokens).

    Thresholds:
      - usagePct >= 80% → suggest /new (close to overflow, /compact too late)
      - 60% <= usagePct < 80% → suggest /compact (still room, but trim)
      - abortedLastRun on a direct session → suggest checking last run
      - kind=direct and idle_min < 5 → no suggestion (currently active)
    """
    out = []
    sessions = sessions_info.get("active") or []
    for sess in sessions:
        # Only suggest for direct (top-level) sessions — subagents are
        # short-lived and their context is reclaimed by the parent.
        if sess.get("kind") != "direct":
            continue
        pct = sess.get("usagePct")
        if pct is None:
            # No token data (session has no successful run yet, or
            # totalTokensFresh=False). Surface abortedLastRun if present.
            if sess.get("abortedLastRun"):
                sid = sess.get("sessionId", "")[:8]
                out.append(
                    f"🟠 主会话 {sid} 上次 run 被 abort — 检查 task_runs.error "
                    f"看是否 FailoverError / 订阅限额;可考虑 /new 重置"
                )
            continue
        sid = sess.get("sessionId", "")[:8]
        if pct >= 80:
            out.append(
                f"🔴 主会话 {sid} 上下文 {pct:.0f}% 已满 — 建议立即 /new "
                f"开新 session;否则下一轮大概率触发 compaction / context overflow"
            )
        elif pct >= 60:
            out.append(
                f"🟡 主会话 {sid} 上下文 {pct:.0f}% — 建议 /compact 压缩历史, "
                f"避免接近窗口上限时被强制 compaction (可能丢上下文)"
            )
    return out


def build_root_cause_summary(result):
    """Build a short root-cause summary for the current time window."""
    s = result["summary"]
    l = result.get("litellm", {})

    ranked = []
    if s["session_stalls"] > 0:
        ranked.append(("任务执行卡住", s["session_stalls"], "active_work_without_progress / stalled_agent_run"))
    if s["llm_aborts"] > 0:
        ranked.append(("LLM 请求中断", s["llm_aborts"], "AbortError / failover"))
    if l.get("auth_errors", 0) > 0:
        ranked.append(("LiteLLM 鉴权失败", l.get("auth_errors", 0), "No api key passed / virtual key 缺失"))
    if l.get("proxy_exceptions", 0) > 0:
        ranked.append(("LiteLLM 代理异常", l.get("proxy_exceptions", 0), "proxy / internal exception"))
    if l.get("upstream_timeouts", 0) > 0:
        ranked.append(("LiteLLM 上游超时", l.get("upstream_timeouts", 0), "upstream timeout"))
    if s["context_overflows"] > 0:
        ranked.append(("上下文溢出", s["context_overflows"], "context overflow / compaction"))
    if s["connection_issues"] > 0:
        ranked.append(("Telegram 连接问题", s["connection_issues"], "fetch timeout / closed before connect"))
    if s.get("transcript_mirror_failures", 0) > 0:
        ranked.append(("会话记录镜像失败", s.get("transcript_mirror_failures", 0), "session file changed mid-turn"))
    if s.get("takeover_silent_gaps", 0) > 0:
        ranked.append(("静默会话记录缺失", s.get("takeover_silent_gaps", 0), "EmbeddedAttemptSessionTakeoverError before mirror"))
    if s.get("reply_session_stale_locks", 0) > 0:
        ranked.append(("Reply session 卡死", s.get("reply_session_stale_locks", 0), "compaction rotated file but reply lock kept stale sessionId"))

    if not ranked:
        return "本窗口未见明显异常，链路整体正常。"

    ranked.sort(key=lambda item: item[1], reverse=True)
    top_name, top_count, top_hint = ranked[0]

    if top_name == "任务执行卡住":
        return (
            f"主要问题是执行层卡住了，共 {top_count} 次。"
            f"这类事件通常是任务还在跑，但长时间没有新进展，"
            f"更像模型/工具流程停在中间，而不是 Telegram 发送失败。"
        )
    if top_name == "LLM 请求中断":
        return (
            f"主要问题是 LLM 请求中断，共 {top_count} 次。"
            f"这通常指向 {top_hint}，优先排查上游模型调用而不是 Telegram。"
        )
    if top_name == "LiteLLM 鉴权失败":
        return (
            f"主要问题是 LiteLLM 鉴权失败，共 {top_count} 次。"
            f"这是 /models 端点收到未带 API key 的请求（No api key passed），"
            f"通常来自本机的模型列表探测/定时刷新，不影响实际的 /chat/completions 聊天链路。"
            f"若确认非本机请求，再排查调用方的 key 配置。"
        )
    if top_name == "LiteLLM 代理异常":
        return (
            f"主要问题是 LiteLLM 代理异常，共 {top_count} 次。"
            f"这更像 proxy / auth / internal error，而不是上下文溢出。"
        )
    if top_name == "LiteLLM 上游超时":
        return (
            f"主要问题是 LiteLLM 上游超时，共 {top_count} 次。"
            f"说明上游响应慢或超时，链路会表现为回复延迟或失败。"
        )
    if top_name == "上下文溢出":
        return (
            f"主要问题是上下文溢出，共 {top_count} 次。"
            f"这会导致 compact / truncate 触发，长任务更容易看起来被打断。"
        )
    if top_name == "会话记录镜像失败":
        return (
            f"主要问题是会话记录镜像失败，共 {top_count} 次。"
            f"消息已送达 Telegram，但 session transcript 因 session file 在 turn 中途被改动"
            f"（通常是 compaction 轮转）而没写入，后续 compaction/上下文回放可能缺失该条。"
        )
    if top_name == "Reply session 卡死":
        return (
            f"主要问题是 Reply session 卡死 (stale lock)，共 {top_count} 次。"
            f"compaction 轮转后 reply session 仍绑定旧 sessionId,锁不释放,"
            f"新进 reply 全部立即冲突,直到 gateway 重启才能恢复。"
            f"Telegram 发送链路本身是好的 (上一轮通常已送达)。"
            f"属于 #88838 范围,#96625 SQLite flip 是真正的修复。"
        )
    return (
        f"主要问题是 Telegram 连接问题，共 {top_count} 次。"
        f"这通常是网络/代理层抖动，不是任务本身逻辑错误。"
    )
