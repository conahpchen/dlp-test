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
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from audit.alerts import read_alerts
from audit.http_latency import samples
from business.atomic_state import read
from business.storage import location
from management.alerts import KINDS, SOURCES, alerts_router
from management.evidence import evidence_router
from management.operations import operations_router
from management.publications import publication_router
from management.templates import template_router
from research.model_budget import usage_today

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
auth = HTTPBasic(auto_error=False)


def require_operator(credentials: HTTPBasicCredentials | None = Depends(auth)):
    token = os.environ.get("HONEYNET_DASHBOARD_PASSWORD", "")
    if not credentials or credentials.username != "operator" or not token or not secrets.compare_digest(
        credentials.password.encode(), token.encode()
    ):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Basic"})


app.include_router(alerts_router(require_operator))
app.include_router(template_router(require_operator))
app.include_router(publication_router(require_operator))
app.include_router(evidence_router(require_operator))
app.include_router(operations_router(require_operator))


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
    hosts = [rows[-1]["payload"] for rows in state.get("facts", {}).get("host", {}).values()]
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
    metrics = (
        ("采样 HTTP 请求", data["http_sample_count"], "最近保留的请求样本"),
        ("HTTP 响应 p95", f"{data['http_response_p95_ms']} ms"
         if data["http_response_p95_ms"] is not None else None, "按保留样本计算"),
        ("Canary 已交付", canary["http_response_sent"], "包含标记的 HTTP 响应"),
        ("跨入口 Canary 命中", sum(value or 0 for value in hits)
         if any(value is not None for value in hits) else None, "SSH、MySQL、模拟 AI"),
        ("DWS 世界版本", data["world_version"], "当前已保存版本"),
    )
    cards = "".join(
        f"<article class='metric'><h2>{title}</h2>"
        f"<strong>{html.escape(str(value)) if value is not None else '—'}</strong>"
        f"<p>{detail}</p></article>"
        for title, value, detail in metrics
    )
    today = datetime.now(timezone.utc).date()
    buckets = Counter(item["recorded_at"][:10] for item in recent)
    chart = [(today - timedelta(days=offset)) for offset in reversed(range(days))]
    peak = max((buckets[day.isoformat()] for day in chart), default=0)
    bars = "".join(
        f'<div class="column"><div class="bar" style="--height:{buckets[day.isoformat()] * 100 / peak if peak else 0:g}%" '
        f'title="{day.isoformat()} · {buckets[day.isoformat()]} 条"></div></div>' for day in chart
    )
    chart_rows = "".join(
        f"<tr><td>{day.isoformat()}</td><td>{buckets[day.isoformat()]}</td></tr>" for day in chart
    )
    alert_rows = "".join(
        f"<tr><td>{html.escape(item['recorded_at'])}</td>"
        f"<td>{html.escape(SOURCES.get(item['source'], item['source']))}</td>"
        f"<td>{html.escape(KINDS.get(item['kind'], item['kind']))}</td></tr>" for item in recent[:6]
    )
    samples_by_status = Counter(data["http_status_groups"])
    total = sum(samples_by_status.values())
    distribution = "".join(
        f'<div class="status"><span>{code}xx</span><meter min="0" max="{total}" value="{samples_by_status[code]}"></meter>'
        f'<span>{samples_by_status[code]}</span></div>' for code in range(2, 6)
    ) if total else "<p>暂无 HTTP 样本</p>"
    services = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in service.values()) + "</tr>"
        for service in data["projected_services"]
    )
    budget = data["model_budget_today"] or {}
    reserved = budget.get("reserved_tokens", "—")
    cost = budget.get("estimated_cost_microusd", "—")
    trend = (
        f'<div class="chart" aria-hidden="true">{bars}</div>'
        f'<div class="axis"><span>{chart[0]}</span><span>{chart[-1]}</span></div>'
        f'<details><summary>查看每日记录</summary><table><thead><tr><th>日期</th><th>记录数</th></tr>'
        f'</thead><tbody>{chart_rows}</tbody></table></details>'
    ) if peak else "<p class='empty'>所选时段暂无告警记录</p>"
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>蜜网运行总览</title>
<style>
:root{{color-scheme:light;--ink:#142b31;--muted:#526970;--line:#d8e6e5;--accent:#116f63}}
*{{box-sizing:border-box}} body{{margin:0;background:#eff5f4;color:var(--ink);font:15px system-ui,sans-serif}}
.layout{{display:grid;grid-template-columns:13rem minmax(0,1fr);min-height:100vh}}
nav{{background:#102e33;color:#e2f0ef;padding:1.6rem 1rem}}
nav b{{display:block;font-size:1.2rem;padding:.7rem;margin-bottom:1rem}}
nav a{{display:block;color:inherit;text-decoration:none;padding:.65rem .75rem;border-radius:.45rem;margin:.2rem 0}}
nav a[aria-current],nav a:hover{{background:#245057;color:white}}
main{{padding:clamp(1rem,3vw,3rem);width:100%;max-width:100rem;margin:auto;min-width:0}}
header,.heading,.axis{{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}}
h1,h2,p{{margin:0}} h1{{font-size:2rem}} h2{{font-size:1rem}} p,summary,.axis{{color:var(--muted)}} p{{margin-top:.5rem}}
a{{color:var(--accent)}} .action,.period a{{display:inline-block;padding:.55rem .85rem;border:1px solid var(--line);border-radius:.45rem;background:white;text-decoration:none}}
.period a[aria-current]{{background:#daf0ed;color:#104e47}} .metrics,.columns{{display:grid;gap:1rem;margin-block:1rem}}
.metrics{{grid-template-columns:repeat(auto-fit,minmax(9rem,1fr))}}
.columns{{grid-template-columns:minmax(0,1.4fr) minmax(0,1fr)}}
article{{background:white;border:1px solid var(--line);border-radius:.75rem;padding:1.2rem;min-width:0}}
.metric strong{{display:block;font-size:1.85rem;margin-top:.6rem;font-variant-numeric:tabular-nums}}
.metric p{{font-size:.8rem}} .chart{{display:flex;height:10rem;gap:.3rem;margin-top:1.5rem;border-bottom:1px solid var(--line);background:repeating-linear-gradient(to top,transparent,transparent calc(25% - 1px),#e9f0ef calc(25% - 1px),#e9f0ef 25%)}}
.column{{flex:1;display:flex;align-items:flex-end;justify-content:center}}
.bar{{height:var(--height);width:min(100%,1.3rem);background:#188b7e;border-radius:.25rem .25rem 0 0}}
.axis,summary{{font-size:.8rem;margin-block:.6rem}} table{{width:100%;border-collapse:collapse;font-size:.88rem}}
td,th{{padding:.65rem .35rem;text-align:left;border-bottom:1px solid var(--line)}} table{{margin-top:.65rem}}
.status{{display:grid;grid-template-columns:3ch 1fr 4ch;gap:.65rem;align-items:center;margin-block:.75rem}}
meter{{width:100%;accent-color:var(--accent)}} .empty{{padding:2rem 0}}
.scroll{{overflow-x:auto}} @media(max-width:820px){{.layout,.columns{{display:block}} nav a{{display:inline-block}} nav b{{margin:0}} .columns article + article{{margin-top:1rem}}}}
</style>
<div class="layout"><nav aria-label="导航"><b>蜜网管理</b><a href="/" aria-current="page">运行总览</a>
<a href="/alerts">告警中心</a><a href="/evidence">会话证据</a><a href="/templates">场景模板</a>
<a href="/publications">数据发布</a><a href="/operations">部署与运行</a></nav>
<main><header><div><h1>运行总览</h1><p>已保存的蜜网交互与运行指标</p></div>
<a class="action" href="/?days={days}">刷新统计</a></header>
<section class="metrics" aria-label="运行指标">{cards}</section>
<article><div class="heading"><h2>告警趋势</h2><div class="period" aria-label="日期范围">
<a href="/?days=7" {'aria-current="true"' if days == 7 else ''}>7 天</a>
<a href="/?days=30" {'aria-current="true"' if days == 30 else ''}>30 天</a></div></div>
<p>UTC，按最近保留的 {len(recent)} 条记录统计</p>{trend}</article>
<section class="columns" aria-label="近期活动"><article><div class="heading"><h2>最近告警</h2>
<a href="/alerts">查看全部</a></div><div class="scroll"><table><thead><tr><th>时间（UTC）</th><th>入口</th><th>事件</th></tr></thead>
<tbody>{alert_rows or '<tr><td colspan="3">暂无告警记录</td></tr>'}</tbody></table></div></article>
<article><h2>HTTP 响应分布</h2>{distribution}<p>今日模型预留 token {reserved}，估算费用 {cost} 微美元</p></article></section>
<article><h2>DWS 服务投影</h2><div class="scroll"><table><thead><tr><th>主机</th><th>协议</th><th>版本</th></tr></thead>
<tbody>{services or '<tr><td colspan="3">暂无投影数据</td></tr>'}</tbody></table></div></article>
</main></div></html>"""
