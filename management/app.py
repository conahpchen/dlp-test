"""Dashboard and template editor on their own authenticated listener."""

import html
import math
import os
import secrets
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from audit.alerts import read_alerts
from audit.http_latency import samples
from business.atomic_state import read
from business.storage import location
from management.alerts import KINDS, SOURCES, alerts_router
from management.evidence import evidence_router
from management.layout import ROOT, metric, page, table
from management.operations import operations_router
from management.publications import publication_router
from management.templates import template_router
from management.world import world_router
from management.situations import situations_router
from management.responses import responses_router
from research.model_budget import usage_today

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
auth = HTTPBasic(auto_error=False)


def _dashboard_role(credentials):
    operator = os.environ.get("HONEYNET_DASHBOARD_PASSWORD", "")
    reviewer = os.environ.get("HONEYNET_DASHBOARD_APPROVER_PASSWORD", "")
    passwords = {"operator": operator, "reviewer": reviewer if reviewer != operator else ""}
    token = passwords.get(credentials.username, "") if credentials else ""
    if token and secrets.compare_digest(credentials.password.encode(), token.encode()):
        return credentials.username
    return None


def require_operator(credentials: HTTPBasicCredentials | None = Depends(auth)):
    if _dashboard_role(credentials) != "operator":
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Basic"})


def require_response_actor(credentials: HTTPBasicCredentials | None = Depends(auth)):
    role = _dashboard_role(credentials)
    if not role:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Basic"})
    return role


app.include_router(alerts_router(require_operator))
app.include_router(template_router(require_operator))
app.include_router(publication_router(require_operator))
app.include_router(evidence_router(require_operator))
app.include_router(operations_router(require_operator))
app.include_router(world_router(require_operator))
app.include_router(situations_router(require_operator))
app.include_router(responses_router(require_response_actor))


@app.get("/dashboard-assets/{name}", dependencies=[Depends(require_response_actor)])
def asset(name: str):
    types = {"console.css": "text/css", "console.js": "text/javascript"}
    if name not in types:
        raise HTTPException(404)
    return FileResponse(ROOT / "assets" / name, media_type=types[name],
                        headers={"X-Content-Type-Options": "nosniff"})



def research_metrics():
    path = os.environ.get("RESEARCH_STATE_FILE")
    if not path or not Path(path).is_file():
        return {"issued": None, "http_response_sent": None, "fake_api_matches": None, "mysql_matches": None, "ssh_matches": None}
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            "issued": db.execute("SELECT COUNT(*) FROM lure_tokens").fetchone()[0]
            if "lure_tokens" in tables else None,
            "http_response_sent": db.execute("SELECT COUNT(*) FROM lure_response_receipts").fetchone()[0]
            if "lure_response_receipts" in tables else None,
            "fake_api_matches": db.execute("SELECT COUNT(*) FROM lure_uses WHERE entry='llm'").fetchone()[0]
            if "lure_uses" in tables else None,
            "mysql_matches": db.execute("SELECT COUNT(*) FROM lure_uses WHERE entry='mysql'").fetchone()[0]
            if "lure_uses" in tables else None,
            "ssh_matches": db.execute("SELECT COUNT(*) FROM lure_uses WHERE entry='ssh'").fetchone()[0]
            if "lure_uses" in tables else None,
        }


def runtime():
    checkpoint = location()
    state = read(checkpoint) if checkpoint and Path(checkpoint).is_file() else {}
    events = state.get("events", [])
    http_samples = samples()
    durations = sorted(duration for _, duration in http_samples)
    private = set(state.get("private_versions", ()))
    hosts = []
    for rows in state.get("facts", {}).get("host", {}).values():
        visible = next((row for row in reversed(rows) if row["world_version"] not in private), None)
        if visible:
            hosts.append(visible["payload"])
    return {
        "canary": research_metrics(),
        "model_budget_today": usage_today(),
        "world_version": state.get("meta", {}).get("world_version"),
        "projected_services": [
            {"host": host["hostname"], "protocol": service.get("proto"), "version": service.get("ver")}
            for host in hosts for service in host.get("services", ())
        ],
        "events_by_entry": dict(Counter(event["entry"] for event in events)),
        "unique_sessions": len({(event["entry"], event["session"]) for event in events}),
        "http_sample_count": len(durations),
        "http_status_groups": dict(Counter(status // 100 for status, _ in http_samples)),
        "http_response_p95_ms": round(durations[math.ceil(.95 * len(durations)) - 1] / 1e6, 3)
        if durations else None,
    }


@app.get("/api/runtime", dependencies=[Depends(require_operator)])
def get_runtime():
    return runtime()


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_operator)])
def dashboard(days: int = Query(7, ge=7, le=30)):
    data = runtime()
    alerts = read_alerts()
    recent = alerts["alerts"]
    canary = data["canary"]
    hits = [canary[key] for key in ("fake_api_matches", "mysql_matches", "ssh_matches")]
    cards = "".join((
        metric("已记录会话", data["unique_sessions"] if data["world_version"] is not None else None,
               "按快照中的入口与会话去重", "mint"),
        metric("HTTP 采样请求", data["http_sample_count"], "近期保留的 HTTP 样本"),
        metric("HTTP 响应 p95", data["http_response_p95_ms"], "按单请求实测样本计算", unit="ms"),
        metric("Canary 已交付", canary["http_response_sent"], "收到响应发送回执的标记", "warm"),
        metric("跨入口 Canary 命中", sum(value or 0 for value in hits)
               if any(value is not None for value in hits) else None, "SSH、MySQL、模拟 AI"),
        metric("DWS 世界版本", data["world_version"], "最近一次保存的世界状态"),
    ))
    today = datetime.now(timezone.utc).date()
    buckets = Counter(item["recorded_at"][:10] for item in recent)
    chart = [(today - timedelta(days=offset)) for offset in reversed(range(days))]
    points = [(day.isoformat(), buckets[day.isoformat()]) for day in chart]
    peak = max((count for _, count in points), default=0)
    bars = "".join(
        f'<div class="column"><div class="bar" style="--height:{count * 100 / peak if peak else 0:g}%" '
        f'title="{day} · {count} 条"></div></div>' for day, count in points
    )
    trend = (
        f'<div class="chart" aria-hidden="true">{bars}</div>'
        f'<div class="axis"><span>{chart[0]}</span><span>{chart[-1]}</span></div>'
        '<details><summary>查看每日记录</summary>' + table(("日期", "告警记录"), points) + "</details>"
    ) if peak else '<p class="empty">所选时段暂无保留告警</p>'
    filters = "".join(f'<a href="/?days={n}"' + (' aria-current="true"' if days == n else "") +
                      f'>近 {n} 天</a>' for n in (7, 14, 30))
    counts = data["http_status_groups"]
    total = sum(counts.values())
    distribution = "".join(
        f'<div class="distribution"><span>{code}xx</span><meter min="0" max="{total}" '
        f'value="{counts.get(code, 0)}" aria-label="{code}xx"></meter>'
        f'<span>{counts.get(code, 0)}</span></div>' for code in range(2, 6)
    ) if total else '<p class="empty">暂无 HTTP 样本</p>'
    alerts_panel = table(("时间（UTC）", "入口", "事件"),
                         ((r["recorded_at"], SOURCES.get(r["source"], r["source"]),
                           KINDS.get(r["kind"], r["kind"])) for r in recent[:7]), "暂无告警记录")
    inventory = "".join(f'<div class="record"><b>{html.escape(s["host"])}</b>'
                        f'<small>{html.escape(str(s["protocol"]))} · {html.escape(str(s["version"]))}</small></div>'
                        for s in data["projected_services"])
    body = f"""
    <section class="metrics" aria-label="关键运行指标">{cards}</section>
    <div class="dashboard-grid">
      <section class="panel"><div class="panel-header"><div><h2>告警趋势</h2>
      <p>按最近保留的 {len(recent)} 条记录统计</p></div>
      <nav class="segments" aria-label="告警趋势范围">{filters}</nav></div>{trend}</section>
      <section class="panel"><div class="panel-header"><div><h2>HTTP 响应分布</h2>
      <p>{total} 条实测请求样本</p></div></div>{distribution}
      <a href="/operations">查看延迟与模型账本 →</a></section>
    </div>
    <div class="dashboard-grid">
      <section class="panel"><div class="panel-header"><h2>最近告警</h2><a href="/alerts">查看全部 →</a></div>
      {alerts_panel}</section>
      <section class="panel"><div class="panel-header"><div><h2>DWS 服务投影</h2><p>已提交的模拟服务</p></div>
      <a href="/world">查看世界 →</a></div><div class="service-overview">{inventory or '暂无投影数据'}</div>
      <div class="status"><a href="/operations">检查实际监听状态 →</a></div></section>
    </div>"""
    return page("/", "运行总览", "查看协议入口、告警和已交付诱饵的运行数据。", body,
                actions=f'<a class="button" href="/?days={days}">刷新统计</a><a class="button primary" href="/chains">查看攻击链路</a>')
