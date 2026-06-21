"""Rendering: CLI text report and the web dashboard HTML template."""

from .config import now_local
from .insights import build_root_cause_summary, build_suggestions
from .util import (
    BOLD, CYAN, DIM, GREEN, RED, YELLOW,
    _session_id_from_key, fmt_duration, parse_ts,
)


def print_report(result, sqlite_info, sessions_info=None):
    s = result["summary"]
    tg_result = result["telegram"]
    l = result["litellm"]

    print()
    print(BOLD("═" * 60))
    print(BOLD("      OpenClaw 调用链路审计报告"))
    print(BOLD("═" * 60))
    print(f"  生成时间:  {now_local().strftime('%Y-%m-%d %H:%M:%S')} (+07:00)")
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
  .controls { margin: 10px 0; display: flex; gap: 10px; align-items: center; }
  .controls select, .controls button { background: #24283b; color: #c0caf5;
    border: 1px solid #334; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
  .controls select:hover, .controls button:hover { background: #334; }
  .refresh { color: #565f89; font-size: 0.85em; margin-left: auto; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 800px) { .grid-2 { grid-template-columns: 1fr; } }
  .section-label { font-size: 0.8em; color: #565f89; margin: 10px 0 5px; }
</style>
</head>
<body>
<div class="container">
  <h1>🔍 OpenClaw + LiteLLM Audit Dashboard</h1>
  <div class="controls">
    <select id="range" onchange="load()">
      <option value="1h" {% if sel=='1h' %}selected{% endif %}>最近1小时</option>
      <option value="3h" {% if sel=='3h' %}selected{% endif %}>最近3小时</option>
      <option value="6h" {% if sel=='6h' %}selected{% endif %}>最近6小时</option>
      <option value="24h" {% if sel=='24h' %}selected{% endif %}>最近24小时</option>
      <option value="today" {% if sel=='today' %}selected{% endif %}>今天</option>
      <option value="yesterday" {% if sel=='yesterday' %}selected{% endif %}>昨天</option>
    </select>
    <button onclick="load()">🔄 刷新</button>
    <span class="refresh">自动刷新: <span id="countdown">30</span>s</span>
  </div>

  <div class="section-label">OpenClaw 网关</div>
  <div class="stats">
    <div class="card">
      <div class="label">Telegram 消息</div>
      <div class="value good">{{ data.summary.telegram_in }}</div>
      <div class="sublabel">回复 {{ data.summary.telegram_out }} 条{% if data.telegram.errors > 0 %} / {{ data.telegram.errors }} ❌{% endif %}</div>
    </div>
    <div class="card">
      <div class="label">执行卡住</div>
      <div class="value {% if data.summary.session_stalls > 0 %}warn{% else %}good{% endif %}">{{ data.summary.session_stalls }}</div>
      <div class="sublabel">active_work_without_progress</div>
    </div>
    <div class="card">
      <div class="label">Failover</div>
      <div class="value {% if data.failovers > 0 %}warn{% else %}good{% endif %}">{{ data.failovers }}</div>
    </div>
    <div class="card">
      <div class="label">上下文溢出</div>
      <div class="value {% if data.context.overflows > 3 %}warn{% else %}good{% endif %}">{{ data.context.overflows }}</div>
      <div class="sublabel">压缩 {{ data.summary.compaction_success }}/{{ data.summary.compaction_success + data.context.compactions.incomplete }}</div>
    </div>
    <div class="card">
      <div class="label">不完整响应</div>
      <div class="value {% if data.incomplete_turns > 0 %}bad{% else %}good{% endif %}">{{ data.incomplete_turns }}</div>
    </div>
    <div class="card">
      <div class="label">Telegram 断连</div>
      <div class="value {% if data.connection_issues > 2 %}warn{% else %}good{% endif %}">{{ data.connection_issues }}</div>
    </div>
    <div class="card">
      <div class="label">Edit 工具失败</div>
      <div class="value {% if data.tool_errors.edit > 10 %}warn{% else %}good{% endif %}">{{ data.tool_errors.edit }}</div>
    </div>
  </div>

  <div class="section-label">LiteLLM 网关</div>
  <div class="stats">
    <div class="card">
      <div class="label">总请求量</div>
      <div class="value good">{{ data.litellm.total_requests }}</div>
      <div class="sublabel">流式 {{ data.litellm.streaming_responses }} 次</div>
    </div>
    <div class="card">
      <div class="label">LLM 中断</div>
      <div class="value {% if data.summary.llm_aborts > 0 %}warn{% else %}good{% endif %}">{{ data.summary.llm_aborts }}</div>
      <div class="sublabel">abort / failover</div>
    </div>
    <div class="card">
      <div class="label">上游超时</div>
      <div class="value {% if data.litellm.upstream_timeouts > 0 %}bad{% else %}good{% endif %}">{{ data.litellm.upstream_timeouts }}</div>
    </div>
    <div class="card">
      <div class="label">上游连接错误</div>
      <div class="value {% if data.litellm.upstream_errors > 0 %}bad{% else %}good{% endif %}">{{ data.litellm.upstream_errors }}</div>
    </div>
    <div class="card">
      <div class="label">鉴权失败</div>
      <div class="value {% if data.litellm.auth_errors > 0 %}bad{% else %}good{% endif %}">{{ data.litellm.auth_errors }}</div>
      <div class="sublabel">no api key passed</div>
    </div>
    <div class="card">
      <div class="label">状态码</div>
      <div class="value" style="font-size: 1em">
        {% for code, cnt in data.litellm.status_codes.items()|sort %}
          <span>{{ code }}: {{ cnt }}</span>{% if not loop.last %} / {% endif %}
        {% endfor %}
      </div>
    </div>
  </div>

  <div class="section-label">LLM 调用延时</div>
  <div class="stats">
    <div class="card">
      <div class="label">平均延时</div>
      <div class="value" style="font-size: 1.2em">{{ data.summary.avg_llm_latency if data.summary.avg_llm_latency else 'N/A' }}</div>
    </div>
    <div class="card">
      <div class="label">P95</div>
      <div class="value" style="font-size: 1.2em">{{ data.summary.p95_llm_latency if data.summary.p95_llm_latency else 'N/A' }}</div>
    </div>
    <div class="card">
      <div class="label">最大</div>
      <div class="value" style="font-size: 1.2em">{{ data.summary.max_llm_latency if data.summary.max_llm_latency else 'N/A' }}</div>
    </div>
    <div class="card">
      <div class="label">LLM 错误/超时</div>
      <div class="value {% if data.summary.llm_errors > 0 or data.summary.llm_timeouts > 0 %}bad{% else %}good{% endif %}">
        {{ data.summary.llm_errors }} / {{ data.summary.llm_timeouts }}
      </div>
    </div>
  </div>

  <div class="section-label">Session 上下文</div>
  <div class="stats">
  {% for sess in data.sessions.active %}
    {% set has_tok = sess.get('hasTokens', False) %}
    {% set pct = sess.usagePct %}
    {% set sess_failed = sess.get('isFailed', False) %}
    {% if sess_failed %}
      {% set cls = 'bad' %}
    {% elif has_tok %}
      {% if pct < 50 %}{% set cls = 'good' %}{% elif pct < 80 %}{% set cls = 'warn' %}{% else %}{% set cls = 'bad' %}{% endif %}
    {% else %}
      {% set cls = '' %}
    {% endif %}
    <div class="card">
      <div class="label">{{ sess.kind }} ({{ sess.model }}) <span style="color:#565f89;font-weight:normal">@{{ sess.clientUpdatedAt if sess.clientUpdatedAt else '??' }}</span> <span style="color:#565f89;font-weight:normal">id:{{ sess.sessionId if sess.sessionId else sess.key }}</span></div>
      <div class="value {{ cls }}">{% if sess_failed %}FAILED{% elif has_tok %}{{ '%.0f'|format(pct) }}%{% else %}N/A{% endif %}</div>
      <div class="sublabel">{% if sess_failed %}❌ 子会话已失败{% elif has_tok %}{{ '{:,}'.format(sess.totalTokens) }}/{{ '{:,}'.format(sess.contextTokens) }}{% else %}N/A/{% endif %}</div>
    </div>
  {% endfor %}
  {% if data.sessions.error %}
    <div class="card">
      <div class="label">Session 查询失败</div>
      <div class="value bad" style="font-size:1em">{{ data.sessions.error }}</div>
    </div>
  {% elif not data.sessions.active %}
    <div class="card"><div class="value" style="font-size:1em">暂无会话数据</div></div>
  {% endif %}
  </div>

  <div class="grid-2">
    <div>
      <h2>🕐 消息分布</h2>
      <div class="bar-chart">
      {% for hour, count in data.time_series|dictsort %}
        {% set max_c = data.time_series.values()|max %}
        {% set pct = (count / max_c * 100)|round if max_c > 0 else 0 %}
        <div class="bar-row">
          <span class="bar-label">{{ hour }}</span>
          <div class="bar-fill" style="width: {{ pct }}%"></div>
          <span class="bar-count">{{ count }}</span>
        </div>
      {% endfor %}
      </div>
    </div>
    <div>
      <h2>💡 建议</h2>
      <div class="events">
      {% for s in suggestions %}
        <div class="event"><span>{{ s }}</span></div>
      {% endfor %}
      </div>
    </div>
  </div>

  <h2>📋 关键事件 (最新优先)</h2>
  <div class="events" id="event-list">
  {% for ev in data.raw_events[:200] %}
    {% set tag_class = 'tag-info' %}
    {% if ev.source == 'litellm' %}{% set tag_class = 'tag-litellm' %}{% endif %}
    {% if ev.level == 'ERROR' %}{% set tag_class = 'tag-error' %}
    {% elif ev.level == 'WARN' %}{% set tag_class = 'tag-warn' %}{% endif %}
    <div class="event">
      <span class="ts">{{ ev.time[:19].replace("T", " ") if ev.time else '??' }}</span>
      <span class="tag {{ tag_class }}">{{ ev.type }}</span>
      <span class="detail">{{ ev.detail[:150] }}</span>
    </div>
  {% endfor %}
  {% if not data.raw_events %}
    <div class="event"><span class="good">✅ 无异常事件</span></div>
  {% elif data.raw_events|length > 200 %}
    <div class="event"><span class="warn">… 还有 {{ data.raw_events|length - 200 }} 条 (用 --since 缩小范围)</span></div>
  {% endif %}
  </div>

  <div style="margin-top: 10px; color: #565f89; font-size: 0.75em;">
    OpenClaw+LiteLLM Audit v1.1 — 数据来源: openclaw-*.log + litellm.err.log
  </div>
</div>
<script>
let timer = 30;
function countdown() {
  timer--;
  document.getElementById('countdown').textContent = timer;
  if (timer <= 0) { timer = 30; load(); }
}
setInterval(countdown, 1000);
async function load() {
  const range = document.getElementById('range').value;
  window.location.href = '/?since=' + range;
}
</script>
</body>
</html>"""
