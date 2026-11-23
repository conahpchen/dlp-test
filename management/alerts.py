"""Authenticated view of recent honeynet alerts."""

from collections import Counter
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from audit.alerts import read_alerts
from management.layout import metric, page, table, table_tools

Source = Literal["web", "ssh", "mysql", "llm"]
Kind = Literal["connection", "login", "login_failed", "canary", "rate_limited", "session_ended"]
SOURCES = {"web": "Web", "ssh": "SSH", "mysql": "MySQL", "llm": "模拟 AI"}
KINDS = {
    "connection": "新连接", "login": "认证成功",
    "login_failed": "认证失败", "canary": "Canary 命中",
    "rate_limited": "入口限速", "session_ended": "连接因超过预算而结束",
}


def alerts_router(require_operator):
    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/api/alerts")
    def data(source: Source | None = None):
        return read_alerts(source)

    @router.get("/alerts", response_class=HTMLResponse)
    def show(source: Source | None = None, kind: Kind | Literal[""] | None = None):
        kind = kind or None
        data = read_alerts(source)
        counts = Counter(item["kind"] for item in data["alerts"])
        cards = "".join(metric(label, counts[key] if data["has_data_source"] else None, "当前入口保留记录",
                               "warm" if key in ("login_failed", "canary") else "")
                        for key, label in KINDS.items())
        events = [item for item in data["alerts"] if kind is None or item["kind"] == kind]
        rows = table(("时间（UTC）", "入口", "事件"),
                     ((r["recorded_at"], SOURCES.get(r["source"], r["source"]),
                       KINDS.get(r["kind"], r["kind"])) for r in events),
                     "暂无匹配告警" if data["has_data_source"] else "尚未收到采集数据")
        links = []
        for key, label in ((None, "全部入口"), *SOURCES.items()):
            query = [f"source={key}"] if key else []
            if kind:
                query.append(f"kind={kind}")
            url = "/alerts" + ("?" + "&amp;".join(query) if query else "")
            current = ' aria-current="true"' if source == key else ""
            links.append(f'<a href="{url}"{current}>{label}</a>')
        selected = "".join(f'<option value="{key}" {"selected" if key == kind else ""}>{label}</option>'
                           for key, label in KINDS.items())
        source_field = f'<input type="hidden" name="source" value="{source}">' if source else ""
        body = f"""<section class="metrics" aria-label="告警统计">{cards}</section>
        <section class="panel"><div class="panel-header"><div><h2>最近告警</h2>
        <p>每个入口保留最近 100 条视图记录。连接可能包含健康探测。</p></div>
        <a href="/canary">查看凭据关联 →</a></div>
        <div class="toolbar"><nav class="segments" aria-label="协议筛选">{"".join(links)}</nav>
        <form action="/alerts" method="get" class="actions">{source_field}
        <label>事件类型 <select name="kind"><option value="">全部事件</option>{selected}</select></label>
        <button type="submit">筛选</button></form></div>
        {table_tools("alert-rows", "搜索时间、入口或事件")}<div id="alert-rows">{rows}</div></section>"""
        return page("/alerts", "蜜网告警", "按协议和事件筛选实际发生的连接、认证和 Canary 命中。", body,
                    actions='<a href="/situations" class="button">查看态势建议</a> <a href="/evidence" class="button">查看会话证据</a>')

    return router
