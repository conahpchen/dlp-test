"""Operator view of retained per-session behavior and linked evidence."""

import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlencode

from audit.flow_evidence import matches
from dws.ids import CROCKFORD
from management.layout import esc, metric, page, table
from research.registration import evidence


ENTRIES = {"web": "Web", "ssh": "SSH", "mysql": "MySQL", "llm": "模拟 AI API"}
EVENT_NAMES = {
    "web.request": "访问 Web 页面或接口",
    "research.visit": "浏览场景",
    "research.delivery": "资料交付记录",
    "research.lead": "提供后续资料线索",
    "research.input": "提交交互请求",
    "research.beacon": "核验链接命中",
    "research.invalid_beacon": "核验链接未通过",
}
TARGET_EVENTS = {"web.request", "research.visit", "research.delivery"}
EVENT_RESULTS = {"research.beacon": "核验通过", "research.invalid_beacon": "核验失败"}
GENERATION_STATUS = {
    "pending": "分析中", "publishing": "等待发布", "applied": "已应用到会话",
    "rejected": "未通过校验", "superseded": "已由更新的结果替代",
}

AGENT_EVIDENCE = {
    "untrusted_agent_self_identification": "请求头出现 Agent 相关标记",
    "multi_target_probe": "访问多个常见探测目标",
    "service_discovery": "探测多个服务端口",
    "model_agent_hypothesis": "已有模型推测有 AI Agent 参与",
}



def _option(value, label, current):
    return f'<option value="{esc(value)}"{" selected" if value == current else ""}>{esc(label)}</option>'


def _link(path, label, **params):
    return f'<a href="{path}?{esc(urlencode(params))}">{esc(label)}</a>'


def _panel(title, content):
    return f'<section class="panel"><div class="panel-header"><h2>{esc(title)}</h2></div>{content}</section>'


def _event_time(identifier):
    # Research records are minted with ulid_now() even when the scenario's logical clock is replayed.
    if not isinstance(identifier, str) or len(identifier) != 26 or identifier[0] not in "01234567":
        return None
    if not all(c in CROCKFORD for c in identifier):
        return None
    milliseconds = 0
    for char in identifier[:10]:
        milliseconds = milliseconds * 32 + CROCKFORD.index(char)
    try:
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _db_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _display_time(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "—"


def _flow_time(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc)


def _network_rows(session):
    try:
        return matches(os.environ.get("HONEYNET_FLOW_STATE_FILE"), {session}).get(session, [])
    except (OSError, sqlite3.Error):
        return None


def _network_panel(flows):
    if flows is None:
        return '<p class="status">网络证据暂不可读取。</p>'
    details = table(("网卡", "起始时间（UTC）", "结束时间（UTC）", "报文数", "字节数"), (
        (row["interface"], _display_time(_flow_time(row["started"])), _display_time(_flow_time(row["ended"])),
         row["packets"], row["bytes"]) for row in flows
    ), "该会话暂无可唯一匹配的 VPC Flow 记录")
    if not flows:
        return details
    packets = sum(row["packets"] for row in flows)
    size = sum(row["bytes"] for row in flows)
    return details + f'<p>上表 {len(flows)} 条匹配记录合计 {packets:,} 个报文、{size:,} 字节。</p>'


def _generation_panel(rows):
    return table(("触发行为", "处理状态", "场景内容", "模拟接口"), (
        (row["business_activity"] or "场景交互", GENERATION_STATUS.get(row["status"], row["status"]),
         "、".join(node["title"] for node in row["nodes"]) or "—",
         "、".join(f'{service["method"]} {service["path"]}' for service in row["services"]) or "—")
        for row in reversed(rows[-10:])
    ), "该会话暂无蜜饵生成记录")


def _agent_panel(assessment, run, session):
    signals = assessment.get("signals", {})
    if assessment.get("suspected"):
        result = "疑似 AI Agent 参与"
        detail = "置信度低。模型引用了当前会话的留存事件。"
    elif {"multi_target_probe", "service_discovery"} & signals.keys():
        result = "Agent 证据不足"
        detail = "观察到自动化探测迹象，客户端类型尚待核实。"
    else:
        result = "Agent 证据不足"
        detail = ("请求头可由客户端自行填写，目前只有弱线索。" if signals
                  else "当前会话暂无可用的 Agent 研判线索。")
    items = table(("线索", "涉及事件数"),
                  ((AGENT_EVIDENCE[kind], len(ids)) for kind, ids in signals.items()), "暂无相关线索")
    evidence_url = '/evidence?' + esc(urlencode({"run": run, "session": session}))
    links = ''.join(f'<li><a href="{evidence_url}#event-{esc(ids[0])}">{esc(AGENT_EVIDENCE[kind])} · 查看事件</a></li>'
                    for kind, ids in signals.items() if ids)
    return f'<h3>{result}</h3><p>{detail}</p>' + items + (f'<ul>{links}</ul>' if links else '')


def profile_page(lab, runs, selected_run, requested_session, chains_for):
    if not selected_run:
        selected_run = next((row["run_id"] for row in runs if row["active"]), runs[0]["run_id"] if runs else None)
    description = "按会话查看来源、访问目标、行为结果和跨入口证据。"
    if not selected_run:
        return page("/profiles", "攻击者画像", description, '<p class="empty">暂无可查看的场景。</p>')

    data = evidence(lab, selected_run, include_registrations=False, include_agent_assessments=True)
    paths = chains_for(selected_run)
    entries = {}
    for row in reversed(data["timeline"]):
        entries.setdefault(row["session"], set()).add(row["entry"])
    for path in paths:
        entries.setdefault(path["source_session"], set()).add(path["source_entry"])
        for hit in path["uses"]:
            entries.setdefault(hit["session"], set()).add(hit["entry"])

    session = requested_session or next(iter(entries), None)
    run_options = ''.join(_option(row["run_id"], ("当前发布 · " if row["active"] else "")
                                  + (row["title"] or "知识库场景") + " · " + row["run_id"][:8], selected_run)
                          for row in runs)
    if selected_run not in {row["run_id"] for row in runs}:
        run_options += _option(selected_run, "历史场景 · " + selected_run[:8], selected_run)
    session_options = _option("", "最近观测会话", session) + ''.join(
        _option(identifier, ' / '.join(ENTRIES.get(entry, entry) for entry in sorted(protocols)) + " · " + identifier, session)
        for identifier, protocols in entries.items()
    )
    controls = f'''<section class="panel"><div class="toolbar">
    <form class="actions" action="/profiles"><label>研究场景 <select name="run">{run_options}</select></label>
    <button type="submit">查看场景</button></form>
    <form class="actions" action="/profiles"><input type="hidden" name="run" value="{esc(selected_run)}">
    <label>观察会话 <select name="session">{session_options}</select></label>
    <button type="submit">查看画像</button></form></div></section>'''
    if session not in entries:
        message = "这个场景暂无会话观测。" if not entries else "所选会话暂无可用记录，请重新选择会话。"
        return page("/profiles", "攻击者画像", description, controls + f'<p class="empty">{message}</p>')

    timeline = [row for row in data["timeline"] if row["session"] == session]
    generation = [row for row in data["generation"] if row["session"] == session]
    hits = [(path, hit) for path in paths for hit in path["uses"]
            if session in (path["source_session"], hit["session"])]
    flows = _network_rows(session)
    protocols = ' / '.join(ENTRIES.get(entry, entry) for entry in sorted(entries[session]))
    prefixes = sorted({row["source_network"] for row in timeline
                       if row["source_network"] and row["source_network"] != "unknown"})

    event_times = {row["event_id"]: _event_time(row["event_id"]) for row in timeline}
    timestamps = list(event_times.values())
    timestamps.extend(_db_time(path["sent_at"]) for path, _ in hits if path["source_session"] == session)
    timestamps.extend(_db_time(hit["observed_at"]) for _, hit in hits if hit["session"] == session)
    for flow in flows or ():
        timestamps.extend((_flow_time(flow["started"]), _flow_time(flow["ended"])))
    timestamps = [value for value in timestamps if value is not None]
    summary = (f'<p class="mono">{esc(session)}</p>'
               + table(("观察入口", "来源网段（脱敏）", "最早证据（UTC）", "最近证据（UTC）"), [
                   (protocols, '、'.join(prefixes) or '暂无记录',
                    _display_time(min(timestamps, default=None)), _display_time(max(timestamps, default=None)))
               ]))

    behavior_rows = [
        (event_times[row["event_id"]], ENTRIES.get(row["entry"], row["entry"]),
         EVENT_NAMES.get(row["kind"], row["kind"]), row["title"] or row["resource"] or "—",
         row["method"] or "—", row["status"] if row["status"] is not None else EVENT_RESULTS.get(row["kind"], "—"))
        for row in timeline
    ]
    for path, hit in hits:
        if hit["session"] == session and hit["entry"] != "web":
            behavior_rows.append((_db_time(hit["observed_at"]), ENTRIES.get(hit["entry"], hit["entry"]),
                                  "Canary 命中", "来自 " + ENTRIES.get(path["source_entry"], path["source_entry"]),
                                  "—", "已关联" if hit["associated"] else "已撤销关联"))
    actions = Counter(row[2] for row in behavior_rows)
    resources = {}
    for row in timeline:
        resource = row["resource"]
        if resource and resource not in {"other", "未识别的场景路径"} and row["kind"] in TARGET_EVENTS:
            key = (row["entry"], resource)
            title, count = resources.get(key, (resource, 0))
            resources[key] = (row["title"] or title, count + 1)
    cross_entries = sum(hit["associated"] and path["source_entry"] != hit["entry"] for path, hit in hits)
    metrics = '<section class="metrics">' + ''.join((
        metric("已记录行为", len(behavior_rows), "Web 事件与其他入口的 Canary 命中记录"),
        metric("访问目标", len(resources) if timeline else None, "按入口和已知资源去重"),
        metric("跨入口 Canary 关联", cross_entries, "交付已确认且关联未撤销"),
        metric("匹配的 VPC Flow", len(flows) if flows is not None else None, "每个会话最多展示 8 条"),
    )) + '</section>'
    counts = table(("行为类别", "记录数"), (
        (kind, count) for kind, count in actions.most_common()
    ), "该会话暂无行为记录")
    targets = table(("入口", "访问或交付目标", "记录数"), (
        (ENTRIES.get(entry, entry), title, count)
        for (entry, _), (title, count) in sorted(resources.items(), key=lambda item: (-item[1][1], item[0]))[:10]
    ), "该会话暂无已记录的 Web 访问目标")
    linkage = table(("来源入口", "来源会话", "命中入口", "命中会话", "命中时间（UTC）", "关联状态"), (
        (ENTRIES.get(path["source_entry"], path["source_entry"]), path["source_session"],
         ENTRIES.get(hit["entry"], hit["entry"]), hit["session"], _display_time(_db_time(hit["observed_at"])),
         "已关联" if hit["associated"] else "已撤销关联") for path, hit in hits
    ), "该会话暂无 Canary 关联")
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    recent = sorted(reversed(behavior_rows), key=lambda row: row[0] or oldest, reverse=True)
    behavior = table(("记录时间（UTC）", "入口", "行为", "请求或交付内容", "方法", "结果"), (
        (_display_time(recorded), entry, action, target, method, result)
        for recorded, entry, action, target, method, result in recent[:10]
    ), "该会话暂无行为明细")

    links = _link('/chains', '打开攻击链路 →', run=selected_run)
    if timeline:
        links += ' ' + _link('/evidence', '打开完整会话证据 →', run=selected_run, session=session)
    body = (controls + metrics + _panel("会话来源与时间", summary)
            + '<div class="dashboard-grid">' + _panel("行为统计", counts) + _panel("访问目标", targets) + '</div>'
            + _panel("最近行为记录", behavior))
    if generation:
        body += _panel("蜜饵生成与处理结果", _generation_panel(generation))
    body += _panel("Canary 跨入口关联", linkage) + _panel("网络证据", _network_panel(flows))
    assessment = data["agent_assessments"].get(session, {})
    body += _panel("Agent 参与研判", _agent_panel(assessment, selected_run, session))
    return page("/profiles", "攻击者画像", description, body, actions=links)
