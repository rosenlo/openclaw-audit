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
        suggestions.append("🔴 LiteLLM 代理异常 — 先查 LitellM / proxy 日志")
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
    if tg_result.get("errors", 0) > 0:
        suggestions.append("🔴 Telegram 回复失败 — 需关注消息发送链路")
    if l.get("warnings", 0) > 0:
        suggestions.append("🟡 Litellm 配置警告 — 将 set_verbose 改为 LITELLM_LOG=DEBUG")
    if not suggestions:
        suggestions.append("✅ 系统运行正常")
    return suggestions


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
    return (
        f"主要问题是 Telegram 连接问题，共 {top_count} 次。"
        f"这通常是网络/代理层抖动，不是任务本身逻辑错误。"
    )
