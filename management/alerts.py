"""Authenticated view of recent honeynet alerts."""

from collections import Counter
from html import escape
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from audit.alerts import read_alerts

Source = Literal["web", "ssh", "mysql", "llm"]
SOURCES = {"web": "OA", "ssh": "SSH", "mysql": "MySQL", "llm": "模拟 AI"}
KINDS = {
    "connection": "新连接", "login": "认证成功",
    "login_failed": "认证失败", "canary": "Canary 命中",
}


def alerts_router(require_operator):
    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/api/alerts")
    def data(source: Source | None = None):
        return read_alerts(source)

    @router.get("/alerts", response_class=HTMLResponse)
    def page(source: Source | None = None):
        data = read_alerts(source)
        rows = "".join(
            f"<tr><td>{escape(item['recorded_at'])}</td>"
            f"<td>{escape(SOURCES.get(item['source'], item['source']))}</td>"
            f"<td>{escape(KINDS.get(item['kind'], item['kind']))}</td></tr>"
            for item in data["alerts"]
        )
        counts = Counter(item["kind"] for item in data["alerts"])
        summary = "".join(
            f"<div><dt>{label}</dt><dd>{counts[key]}</dd></div>"
            for key, label in KINDS.items()
        )
        empty = "暂无匹配事件" if data["has_data_source"] else "尚未收到采集数据"
        filters = "".join(f'<a href="/alerts?source={key}">{label}</a>' for key, label in SOURCES.items())
        return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>蜜网告警</title>
<style>
body{{font:16px system-ui;background:#eef2f5;color:#17283f;max-width:66rem;margin:auto;padding:2rem}}
nav{{display:flex;gap:1rem;flex-wrap:wrap;margin-block:1rem}}
dl{{display:grid;grid-template-columns:repeat(auto-fit,minmax(8rem,1fr));gap:.75rem}}
dl div{{background:white;padding:1rem;border-radius:.5rem}} dd{{margin:.25rem 0 0;font-size:1.5rem}}
a{{color:#2258a6}} table{{width:100%;background:white;border-collapse:collapse}}
td,th{{padding:.75rem;text-align:left;border-bottom:1px solid #dae2ea}}
</style><main><nav><a href="/">运行总览</a><a href="/evidence">会话证据</a></nav>
<h1>蜜网告警</h1><p>连接、认证和 Canary 命中来自实际交互。列表最多显示最近 100 条，连接记录可能包含健康探测，访客身份需另行复核。</p>
<nav aria-label="入口筛选"><a href="/alerts">全部</a>{filters}
<a href="/alerts{('?source=' + source) if source else ''}">刷新</a></nav>
<dl aria-label="当前筛选下的最近记录">{summary if data["has_data_source"] else ""}</dl>
<table><thead><tr><th>记录时间（UTC）</th><th>入口</th><th>事件</th></tr></thead>
<tbody>{rows or '<tr><td colspan="3">' + empty + '</td></tr>'}</tbody></table></main></html>"""

    return router
