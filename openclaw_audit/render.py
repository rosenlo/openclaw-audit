"""Rendering: CLI text report and the web dashboard HTML template."""

from .config import LOCAL_TZ, now_local
from .insights import build_root_cause_summary, build_suggestions
from .util import (
    BOLD, CYAN, DIM, GREEN, RED, YELLOW,
    _session_id_from_key, fmt_duration, parse_ts, tz_offset_str,
)


# ─── Web fragment helpers ─────────────────────────────────────────────
# Each section of the dashboard is rendered by a Python function that
# returns an HTML string. The initial Jinja template and the
# /api/fragments endpoint share these so the first paint and every
# fetch-based refresh produce identical markup — no formatting logic
# duplicated between server and client JS.

def format_latency(sec):
    """Format a latency in seconds for display (e.g. 12.3 -> '12.3s').
    Delegates to fmt_duration so CLI and Web render identically."""
    return fmt_duration(sec)


def format_event_time(ts_str):
    """Format an event timestamp as 'YYYY-MM-DD HH:MM:SS' in LOCAL_TZ.

    Replaces the previous naive ``ev.time[:19].replace('T', ' ')`` which
    broke for text-format LiteLLM lines (no date) and any non-ISO shape.
    Falls back to the raw string (truncated) when parsing fails.
    """
    if not ts_str:
        return "??"
    pt = parse_ts(ts_str)
    if pt:
        return pt.strftime("%Y-%m-%d %H:%M:%S")
    return ts_str[:19].replace("T", " ") if ts_str else "??"


def _cls_for_count(count, warn_at=1, bad_at=None):
    """Return 'good'/'warn'/'bad' CSS class for a count-based card."""
    if count == 0:
        return "good"
    if bad_at is not None and count >= bad_at:
        return "bad"
    if count >= warn_at:
        return "warn"
    return "good"


def _card(label, value, sublabel="", value_cls="", value_style=""):
    """Render a single dashboard card."""
    sub_html = f'<div class="sublabel">{sublabel}</div>' if sublabel else ""
    cls_attr = f' class="value {value_cls}"' if value_cls else ' class="value"'
    style_attr = f' style="{value_style}"' if value_style else ""
    return (
        f'<div class="card">'
        f'<div class="label">{label}</div>'
        f'<div{cls_attr}{style_attr}>{value}</div>'
        f'{sub_html}'
        f'</div>'
    )


def render_stats_openclaw(data):
    s = data["summary"]
    tg = data["telegram"]
    err_suffix = f' / {tg["errors"]} ❌' if tg["errors"] > 0 else ""
    return "".join([
        _card("Telegram 消息", s["telegram_in"],
              f'回复 {s["telegram_out"]} 条{err_suffix}', "good"),
        _card("执行卡住", s["session_stalls"],
              "active_work_without_progress",
              _cls_for_count(s["session_stalls"])),
        _card("Failover", data["failovers"], "",
              _cls_for_count(data["failovers"])),
        _card("上下文溢出", data["context"]["overflows"],
              f'压缩 {s["compaction_success"]}/{s["compaction_success"] + data["context"]["compactions"]["incomplete"]}',
              "bad" if data["context"]["overflows"] > 3 else "good"),
        _card("不完整响应", data["incomplete_turns"], "",
              _cls_for_count(data["incomplete_turns"], bad_at=1)),
        _card("Telegram 断连", data["connection_issues"], "",
              "warn" if data["connection_issues"] > 2 else "good"),
        _card("Edit 工具失败", data["tool_errors"]["edit"], "",
              "warn" if data["tool_errors"]["edit"] > 10 else "good"),
    ])


def render_stats_litellm(data):
    s = data["summary"]
    l = data["litellm"]
    codes_str = ", ".join(f"{c}: {n}" for c, n in sorted(l["status_codes"].items()))
    return "".join([
        _card("总请求量", l["total_requests"],
              f'流式 {l["streaming_responses"]} 次', "good"),
        _card("LLM 中断", s["llm_aborts"], "abort / failover",
              _cls_for_count(s["llm_aborts"])),
        _card("上游超时", l["upstream_timeouts"], "",
              _cls_for_count(l["upstream_timeouts"], bad_at=1)),
        _card("上游连接错误", l["upstream_errors"], "",
              _cls_for_count(l["upstream_errors"], bad_at=1)),
        _card("鉴权失败", l["auth_errors"], "no api key passed",
              _cls_for_count(l["auth_errors"], bad_at=1)),
        _card("状态码", codes_str, "", "", "font-size: 1em; flex-wrap: wrap; display: flex; gap: 4px;"),
    ])


def render_stats_latency(data):
    s = data["summary"]
    avg = format_latency(s.get("avg_llm_latency"))
    p95 = format_latency(s.get("p95_llm_latency"))
    mx = format_latency(s.get("max_llm_latency"))
    err_cls = "bad" if (s["llm_errors"] > 0 or s["llm_timeouts"] > 0) else "good"
    return "".join([
        _card("平均延时", avg, "", "", "font-size: 1.2em"),
        _card("P95", p95, "", "", "font-size: 1.2em"),
        _card("最大", mx, "", "", "font-size: 1.2em"),
        _card("LLM 错误/超时", f'{s["llm_errors"]} / {s["llm_timeouts"]}', "", err_cls),
    ])


def render_stats_sessions(sessions_info):
    """Render session context cards. sessions_info has 'active' list or 'error'."""
    if sessions_info.get("error"):
        return _card("Session 查询失败", sessions_info["error"], "", "bad", "font-size:1em")
    sessions = sessions_info.get("active", [])
    if not sessions:
        return _card("", "暂无会话数据", "", "", "font-size:1em")
    cards = []
    for sess in sessions:
        has_tok = sess.get("hasTokens", False)
        pct = sess.get("usagePct")
        sess_failed = sess.get("isFailed", False)
        if sess_failed:
            cls = "bad"
            value = "FAILED"
            sub = "❌ 子会话已失败"
        elif has_tok:
            if pct < 50:
                cls = "good"
            elif pct < 80:
                cls = "warn"
            else:
                cls = "bad"
            value = f"{pct:.0f}%"
            sub = f'{sess["totalTokens"]:,}/{sess["contextTokens"]:,}'
        else:
            cls = ""
            value = "N/A"
            sub = f'N/A/{sess.get("contextTokens", 0):,}'
        label = (f'{sess["kind"]} ({sess["model"]}) '
                 f'<span style="color:#565f89;font-weight:normal">@{sess.get("clientUpdatedAt") or "??"}</span> '
                 f'<span style="color:#565f89;font-weight:normal">id:{sess.get("sessionId") or sess.get("key", "")}</span>')
        cards.append(_card(label, value, sub, cls))
    return "".join(cards)


def render_time_series(time_series):
    if not time_series:
        return '<div class="event"><span class="good">无数据</span></div>'
    max_c = max(time_series.values()) if time_series else 1
    rows = []
    for hour, count in sorted(time_series.items()):
        pct = (count / max_c * 100) if max_c > 0 else 0
        rows.append(
            f'<div class="bar-row">'
            f'<span class="bar-label">{hour}</span>'
            f'<div class="bar-fill" style="width: {pct:.0f}%"></div>'
            f'<span class="bar-count">{count}</span>'
            f'</div>'
        )
    return "".join(rows)


def render_suggestions(suggestions):
    if not suggestions:
        return '<div class="event"><span class="good">✅ 系统运行正常</span></div>'
    return "".join(f'<div class="event"><span>{s}</span></div>' for s in suggestions)


def render_events(events):
    """Render the event list (already newest-first)."""
    if not events:
        return '<div class="event"><span class="good">✅ 无异常事件</span></div>'
    rows = []
    for ev in events[:200]:
        tag_class = "tag-info"
        if ev.get("source") == "litellm":
            tag_class = "tag-litellm"
        level = ev.get("level", "")
        if level == "ERROR":
            tag_class = "tag-error"
        elif level == "WARN":
            tag_class = "tag-warn"
        ts_display = format_event_time(ev.get("time", ""))
        detail = (ev.get("detail") or "")[:150]
        rows.append(
            f'<div class="event">'
            f'<span class="ts">{ts_display}</span>'
            f'<span class="tag {tag_class}">{ev.get("type", "")}</span>'
            f'<span class="detail">{detail}</span>'
            f'</div>'
        )
    if len(events) > 200:
        rows.append(
            f'<div class="event"><span class="warn">… 还有 {len(events) - 200} 条 (用 --since 缩小范围)</span></div>'
        )
    return "".join(rows)


def render_section_fragments(result, suggestions, sessions_info):
    """Return a dict of {container_id: html_string} for /api/fragments.

    Used by the web dashboard's fetch-based refresh: JS receives this dict
    and sets innerHTML on each container by id. Must stay in sync with the
    container IDs in HTML_TEMPLATE.
    """
    return {
        "stats-openclaw": render_stats_openclaw(result),
        "stats-litellm": render_stats_litellm(result),
        "stats-latency": render_stats_latency(result),
        "stats-sessions": render_stats_sessions(sessions_info),
        "time-series": render_time_series(result.get("time_series", {})),
        "suggestions": render_suggestions(suggestions),
        "event-list": render_events(result.get("raw_events", [])),
    }


def print_report(result, sqlite_info, sessions_info=None):
    s = result["summary"]
    tg_result = result["telegram"]
    l = result["litellm"]

    print()
    print(BOLD("═" * 60))
    print(BOLD("      OpenClaw 调用链路审计报告"))
    print(BOLD("═" * 60))
    print(f"  生成时间:  {now_local().strftime('%Y-%m-%d %H:%M:%S')} ({tz_offset_str(LOCAL_TZ)})")
    print(f"  数据来源:  OpenClaw日志 + Litellm日志")

    # ── Health Score ──
    score = 100
    deductions = []
    if s["llm_timeouts"] > 0:
        score -= min(s["llm_timeouts"] * 15, 60)
        deductions.append(f"LLM超时 {s['llm_timeouts']}次")
    if s["llm_aborts"] > 0:
        score -= min(s["llm_aborts"] * 8, 24)
        deductions.append(f"LLM中断 {s['llm_aborts']}次")
    if s["litellm_upstream_timeouts"] > 0:
        score -= min(s["litellm_upstream_timeouts"] * 10, 40)
        deductions.append(f"Litellm上游超时 {s['litellm_upstream_timeouts']}次")
    if s["session_stalls"] > 0:
        score -= min(s["session_stalls"] * 6, 24)
        deductions.append(f"任务卡住 {s['session_stalls']}次")
    if s["failovers"] > 0:
        score -= min(s["failovers"] * 5, 20)
    if s["incomplete_turns"] > 0:
        score -= min(s["incomplete_turns"] * 10, 20)
    if s["context_overflows"] > 5:
        score -= 5
    if s["connection_issues"] > 3:
        score -= 5
    score = max(score, 0)
    score_color = GREEN if score >= 80 else (YELLOW if score >= 50 else RED)
    print()
    print(f"  健康评分:  {score_color(f'{score}/100')}")
    if deductions:
        reasons = ", ".join(deductions)
        print(f"  {DIM(f'扣分原因: {reasons}')}")

    # ── 指标 ──
    print()
    print(BOLD("  📊 总体指标"))
    print(f"     ├─ Telegram 消息:        {CYAN(str(s['telegram_in']))} 条")
    _send_ok = s.get("telegram_send_ok", 0)
    # send_ok counts real Telegram replies across both send paths (queued
    # "outbound send ok" + direct "sendRichMessage ok"). telegram_out counts
    # the "message processed" diagnostic line, which in practice is almost
    # always a cron-job timeout error (channel=cron), NOT a reply — so it is
    # reported as "处理异常" rather than as replies.
    print(f"     ├─ Telegram 回复:        {GREEN(str(_send_ok)) if _send_ok else '0'} 条 (处理异常: {RED(str(s['telegram_out'])) if s['telegram_out'] else '0'}, 发送错误: {RED(str(tg_result['errors'])) if tg_result['errors'] else '0'})")
    _tmf = s.get("transcript_mirror_failures", 0)
    print(f"     ├─ 会话记录缺失:          {YELLOW(str(_tmf)) if _tmf else GREEN('0')} 条 (送达成功但 transcript 镜像失败)")
    print(f"     ├─ LLM 调用错误:         {RED(str(s['llm_errors'])) if s['llm_errors'] else GREEN('0')} 次")
    print(f"     ├─ LLM 中断:             {YELLOW(str(s['llm_aborts'])) if s['llm_aborts'] else GREEN('0')} 次")
    print(f"     ├─ LLM 超时:             {RED(str(s['llm_timeouts'])) if s['llm_timeouts'] else GREEN('0')} 次")
    print(f"     ├─ 执行卡住:             {YELLOW(str(s['session_stalls'])) if s['session_stalls'] else GREEN('0')} 次")
    print(f"     ├─ Failover:             {YELLOW(str(s['failovers'])) if s['failovers'] else GREEN('0')} 次")
    print(f"     ├─ 上下文溢出:            {YELLOW(str(s['context_overflows'])) if s['context_overflows'] else GREEN('0')} 次")
    print(f"     ├─ 上下文压缩成功率:      {s['compaction_success']}/{s['compaction_success'] + result['context']['compactions']['incomplete']}")
    print(f"     ├─ 不完整响应:            {RED(str(s['incomplete_turns'])) if s['incomplete_turns'] else GREEN('0')} 次")
    print(f"     ├─ Telegram断连:          {YELLOW(str(s['connection_issues'])) if s['connection_issues'] else GREEN('0')} 次")
    print(f"     ├─ 配置热加载:            {s['config_reloads']} 次")
    print(f"     └─ Edit工具失败:          {YELLOW(str(s['edit_fails'])) if s['edit_fails'] else GREEN('0')} 次")

    # ── LiteLLM 指标 ──
    print()
    print(BOLD("  🔌 LiteLLM 网关状态"))
    codes_str = ", ".join(f"{c}: {n}" for c, n in sorted(l["status_codes"].items()))
    print(f"     ├─ 总请求量:              {l['total_requests']} 次")
    print(f"     ├─ 流式响应:              {l['streaming_responses']} 次")
    print(f"     ├─ 状态码分布:            {codes_str}")
    print(f"     ├─ 上游超时:              {RED(str(l['upstream_timeouts'])) if l['upstream_timeouts'] else GREEN('0')} 次")
    print(f"     ├─ 上游连接错误:          {RED(str(l['upstream_connections'])) if l['upstream_connections'] else GREEN('0')} 次")
    print(f"     ├─ Fallback失败:          {RED(str(l['fallback_failures'])) if l['fallback_failures'] else GREEN('0')} 次")
    print(f"     ├─ 代理异常:              {RED(str(l['proxy_exceptions'])) if l['proxy_exceptions'] else GREEN('0')} 次")
    print(f"     ├─ 鉴权失败:              {RED(str(l['auth_errors'])) if l['auth_errors'] else GREEN('0')} 次")
    print(f"     └─ 上游错误总数:          {RED(str(l['upstream_errors'])) if l['upstream_errors'] else GREEN('0')} 次")

    if l["warnings"]:
        print(f"     └─ 配置警告:              {YELLOW(str(l['warnings']))} 次 (set_verbose 已弃用)")

    # ── LLM 延时 ──
    print()
    print(BOLD("  ⏱  LLM 调用延时"))
    if s.get("avg_llm_latency"):
        avg = s["avg_llm_latency"]
        p95 = s.get("p95_llm_latency", 0)
        max_l = s.get("max_llm_latency", 0)
        print(f"     ├─ 平均:    {fmt_duration(avg)}")
        print(f"     ├─ P95:     {fmt_duration(p95)}")
        print(f"     └─ 最大:    {fmt_duration(max_l)}")
    else:
        print(f"     └─ {'(无数据)' if s['llm_errors'] == 0 else '(仅错误, 无成功调用)'}")

    # ── 按小时分布 ──
    if result["time_series"]:
        print()
        print(BOLD("  🕐 消息量按小时分布"))
        max_count = max(result["time_series"].values()) or 1
        for hour, count in sorted(result["time_series"].items()):
            bar = "█" * int(count / max_count * 30)
            print(f"     {hour}  {bar} {count}")

    # ── SQLite ──
    if sqlite_info:
        print()
        print(BOLD("  🗄️  数据库 (SQLite)"))
        if "subagent_count" in sqlite_info:
            print(f"     ├─ 子Agent调用:     {sqlite_info['subagent_count']} 次")
            print(f"     ├─ 平均耗时:        {fmt_duration(sqlite_info.get('subagent_avg_dur', 0))}")
            print(f"     └─ 最大耗时:        {fmt_duration(sqlite_info.get('subagent_max_dur', 0))}")
        if "flows" in sqlite_info:
            print(f"     └─ Flow状态:        {sqlite_info['flows']}")
        if "ingress" in sqlite_info:
            print(f"     └─ Telegram入口:    {sqlite_info['ingress']}")

    # ── Session 上下文用量 ──
    if sessions_info and sessions_info.get("active"):
        print()
        print(BOLD("  💬 Session 上下文用量"))
        for sess in sessions_info["active"]:
            pct = sess.get("usagePct")
            has_tokens = sess.get("hasTokens", False)
            status = sess.get("status", "unknown")
            kind = sess["kind"]
            model = sess["model"]
            updated_display = sess.get("clientUpdatedAt", "")
            timestamp_tag = f" {DIM(chr(64) + updated_display)}" if updated_display else ""
            sess_id = sess.get("sessionId", "") or _session_id_from_key(sess.get("key", ""))
            # 失败会话优先标记
            if sess.get("isFailed"):
                print(f"     [{kind:8}] {RED('❌ FAILED')} {'—'} {sess.get('totalTokens', '?')}/{sess['contextTokens']:,} {model}{timestamp_tag}  {DIM(f'id:{sess_id}')}")
            elif not has_tokens:
                print(f"     [{kind:8}] {'N/A':>5} {'—'} {sess.get('totalTokens', '?')}/{sess['contextTokens']:,} {model}{timestamp_tag}  {DIM(f'id:{sess_id}')}")
            else:
                pct_val = pct or 0
                bar_len = int(pct_val / 100 * 25)
                bar = "█" * bar_len
                color_f = GREEN if pct_val < 50 else (YELLOW if pct_val < 80 else RED)
                total_display = f"{sess['totalTokens']:,}"
                ctx_display = f"{sess['contextTokens']:,}"
                print(f"     [{kind:8}] {color_f(f'{pct_val:5.1f}%')} {bar} {total_display}/{ctx_display} {model}{timestamp_tag}  {DIM(f'id:{sess_id}')}")

    # ── 关键事件 ──
    events = result.get("raw_events", [])
    if events:
        print()
        print(BOLD("  📋 关键事件列表 (最新优先)"))
        limit = 100
        for ev in events[:limit]:
            pt = parse_ts(ev["time"])
            ts_display = pt.strftime("%Y-%m-%d %H:%M:%S") if pt else (ev["time"][:19].replace("T", " ") if ev["time"] else "??")
            src = ev.get("source", "?")
            if src == "litellm":
                src_tag = DIM("[L]")
            else:
                src_tag = ""
            detail = ev.get("detail", "")[:200]
            level = ev.get("level", "")
            prefix = RED("✗") if level == "ERROR" else (YELLOW("!") if level == "WARN" else "·")
            print(f"     {prefix} {DIM(ts_display)} {src_tag} {ev['type']}")
            if detail:
                print(f"       {DIM(detail)}")

        if len(events) > limit:
            print(f"     {DIM(f'... 还有 {len(events) - limit} 条事件 (用 --since 缩小范围)')}")

    # ── 建议 ──
    print()
    print(BOLD("  💡 建议"))
    suggestions = build_suggestions(result, tg_result)
    print(f"     {build_root_cause_summary(result)}")

    for sug in suggestions:
        print(f"     {sug}")
    print()
    print(BOLD("═" * 60))
    print()


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenClaw Audit Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, 'SF Mono', Menlo, monospace;
         background: #1a1b26; color: #c0caf5; padding: 20px; }
  .container { max-width: 1200px; margin: 0 auto; }
  h1 { color: #7aa2f7; font-size: 1.5em; margin-bottom: 20px;
       border-bottom: 1px solid #334; padding-bottom: 10px; }
  h2 { color: #89ddff; font-size: 1.1em; margin: 20px 0 10px; }
  .stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px,1fr));
           gap: 10px; margin-bottom: 20px; }
  .card { background: #24283b; border-radius: 8px; padding: 12px;
          border: 1px solid #334; }
  .card .label { font-size: 0.75em; color: #565f89; }
  .card .value { font-size: 1.5em; font-weight: bold; margin: 4px 0; }
  .card .sublabel { font-size: 0.7em; color: #565f89; }
  .card-section { font-size: 0.65em; color: #565f89; margin-top: 6px;
                   padding-top: 6px; border-top: 1px solid #334; }
  .good { color: #9ece6a; } .warn { color: #e0af68; } .bad { color: #f7768e; }
  .events { background: #24283b; border-radius: 8px; padding: 12px;
            border: 1px solid #334; max-height: 600px; overflow-y: auto; }
  .events::-webkit-scrollbar { width: 6px; }
  .events::-webkit-scrollbar-thumb { background: #334; border-radius: 3px; }
  .event { padding: 3px 0; border-bottom: 1px solid #1a1b26;
           font-size: 0.82em; display: flex; gap: 8px; align-items: baseline; }
  .event .ts { color: #565f89; white-space: nowrap; font-size: 0.9em; }
  .event .tag { padding: 1px 6px; border-radius: 3px; white-space: nowrap;
                font-size: 0.9em; }
  .tag-litellm { background: #bb9af722; color: #bb9af7; }
  .tag-error { background: #f7768e22; color: #f7768e; }
  .tag-warn { background: #e0af6822; color: #e0af68; }
  .tag-info { background: #7aa2f722; color: #7aa2f7; }
  .event .detail { color: #a9b1d6; overflow: hidden; text-overflow: ellipsis; }
  .bar-chart { margin: 10px 0; }
  .bar-row { display: flex; align-items: center; gap: 8px; margin: 2px 0; }
  .bar-label { width: 40px; text-align: right; font-size: 0.8em; color: #565f89; }
  .bar-fill { height: 18px; background: #7aa2f7; border-radius: 3px;
              min-width: 4px; transition: width 0.3s; }
  .bar-count { font-size: 0.8em; color: #565f89; }
  .controls { margin: 10px 0; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .controls select, .controls button { background: #24283b; color: #c0caf5;
    border: 1px solid #334; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
  .controls select:hover, .controls button:hover { background: #334; }
  .controls button:disabled { opacity: 0.5; cursor: wait; }
  .refresh { color: #565f89; font-size: 0.85em; margin-left: auto; }
  .last-updated { color: #565f89; font-size: 0.8em; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 800px) { .grid-2 { grid-template-columns: 1fr; } }
  .section-label { font-size: 0.8em; color: #565f89; margin: 10px 0 5px; }
  #error-banner { display: none; background: #f7768e22; color: #f7768e;
                  border: 1px solid #f7768e; padding: 8px 12px; border-radius: 6px;
                  margin-bottom: 12px; font-size: 0.85em; }
  #error-banner.visible { display: block; }
  .spinner { display: inline-block; width: 12px; height: 12px;
             border: 2px solid #565f89; border-top-color: transparent;
             border-radius: 50%; animation: spin 0.8s linear infinite;
             vertical-align: middle; margin-right: 4px; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="container">
  <h1>🔍 OpenClaw + LiteLLM Audit Dashboard</h1>
  <div class="controls">
    <select id="range" onchange="onRangeChange()">
      <option value="1h" {% if sel=='1h' %}selected{% endif %}>最近1小时</option>
      <option value="3h" {% if sel=='3h' %}selected{% endif %}>最近3小时</option>
      <option value="6h" {% if sel=='6h' %}selected{% endif %}>最近6小时</option>
      <option value="24h" {% if sel=='24h' %}selected{% endif %}>最近24小时</option>
      <option value="today" {% if sel=='today' %}selected{% endif %}>今天</option>
      <option value="yesterday" {% if sel=='yesterday' %}selected{% endif %}>昨天</option>
    </select>
    <button id="refresh-btn" onclick="onRefreshClick()">🔄 刷新</button>
    <span class="refresh">自动刷新: <span id="countdown">30</span>s</span>
    <span class="last-updated">最后更新: <span id="last-updated">{{ generated_at }}</span></span>
  </div>

  <div id="error-banner"></div>

  <div class="section-label">OpenClaw 网关</div>
  <div class="stats" id="stats-openclaw">{{ stats_openclaw|safe }}</div>

  <div class="section-label">LiteLLM 网关</div>
  <div class="stats" id="stats-litellm">{{ stats_litellm|safe }}</div>

  <div class="section-label">LLM 调用延时</div>
  <div class="stats" id="stats-latency">{{ stats_latency|safe }}</div>

  <div class="section-label">Session 上下文</div>
  <div class="stats" id="stats-sessions">{{ stats_sessions|safe }}</div>

  <div class="grid-2">
    <div>
      <h2>🕐 消息分布</h2>
      <div class="bar-chart" id="time-series">{{ time_series_html|safe }}</div>
    </div>
    <div>
      <h2>💡 建议</h2>
      <div class="events" id="suggestions">{{ suggestions_html|safe }}</div>
    </div>
  </div>

  <h2>📋 关键事件 (最新优先)</h2>
  <div class="events" id="event-list">{{ events_html|safe }}</div>

  <div style="margin-top: 10px; color: #565f89; font-size: 0.75em;">
    OpenClaw+LiteLLM Audit v1.2 — fetch-based refresh, no full page reload
  </div>
</div>
<script>
const REFRESH_INTERVAL = 30;
let timer = REFRESH_INTERVAL;
let fetching = false;

function countdown() {
  if (document.hidden) return;
  timer--;
  document.getElementById('countdown').textContent = timer;
  if (timer <= 0) {
    timer = REFRESH_INTERVAL;
    fetchData();
  }
}
setInterval(countdown, 1000);

function onRangeChange() {
  // Reset countdown when user changes the range, then fetch immediately.
  timer = REFRESH_INTERVAL;
  document.getElementById('countdown').textContent = timer;
  fetchData();
}

function onRefreshClick() {
  if (fetching) return;
  timer = REFRESH_INTERVAL;
  fetchData();
}

function setLoading(loading) {
  fetching = loading;
  const btn = document.getElementById('refresh-btn');
  btn.disabled = loading;
  btn.innerHTML = loading
    ? '<span class="spinner"></span>加载中...'
    : '🔄 刷新';
}

function showError(msg) {
  const banner = document.getElementById('error-banner');
  banner.textContent = '⚠️ ' + msg;
  banner.classList.add('visible');
}

function hideError() {
  document.getElementById('error-banner').classList.remove('visible');
}

async function fetchData() {
  if (fetching) return;
  setLoading(true);
  try {
    const range = document.getElementById('range').value;
    const res = await fetch('/api/fragments?since=' + encodeURIComponent(range));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (data.error) { showError(data.error); return; }

    // Save scroll positions before patching DOM (innerHTML resets scroll).
    const eventList = document.getElementById('event-list');
    const savedScrolls = {
      'event-list': eventList.scrollTop,
      'suggestions': document.getElementById('suggestions').scrollTop,
    };
    const savedPageScroll = window.scrollY;

    // Patch each section.
    const sections = ['stats-openclaw', 'stats-litellm', 'stats-latency',
                      'stats-sessions', 'time-series', 'suggestions', 'event-list'];
    for (const id of sections) {
      if (data[id] !== undefined) {
        document.getElementById(id).innerHTML = data[id];
      }
    }

    // Restore scroll positions (innerHTML resets scroll to 0).
    eventList.scrollTop = savedScrolls['event-list'];
    document.getElementById('suggestions').scrollTop = savedScrolls['suggestions'];
    // Page scroll: only restore if user was scrolled to bottom area (avoid
    // fighting user scrolling up during the brief fetch window).
    if (savedPageScroll > 0) {
      window.scrollTo(0, savedPageScroll);
    }

    // Update "last updated" timestamp.
    if (data.generated_at) {
      document.getElementById('last-updated').textContent = data.generated_at;
    }
    hideError();
  } catch (e) {
    showError('刷新失败: ' + e.message);
  } finally {
    setLoading(false);
  }
}

// Pause auto-refresh when tab is hidden (saves CPU + server work).
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    // On return, reset countdown so user gets fresh data soon.
    timer = Math.min(timer, 5);
  }
});
</script>
</body>
</html>"""
