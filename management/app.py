"""Dashboard and template editor on their own authenticated listener."""

import html
import math
import os
import secrets
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from audit.http_latency import samples
from business.atomic_state import read
from business.storage import location
from management.publications import publication_router
from management.templates import template_router

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
auth = HTTPBasic(auto_error=False)


def require_operator(credentials: HTTPBasicCredentials | None = Depends(auth)):
    token = os.environ.get("HONEYNET_DASHBOARD_PASSWORD", "")
    if not credentials or credentials.username != "operator" or not token or not secrets.compare_digest(
        credentials.password.encode(), token.encode()
    ):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Basic"})


app.include_router(template_router(require_operator))
app.include_router(publication_router(require_operator))


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
    durations = sorted(duration for _, duration in samples())
    hosts = [rows[-1]["payload"] for rows in state.get("facts", {}).get("host", {}).values()]
    return {
        "canary": research_metrics(),
        "world_version": state.get("meta", {}).get("world_version"),
        "projected_services": [
            {"host": host["hostname"], "protocol": service.get("proto"), "version": service.get("ver")}
            for host in hosts for service in host.get("services", ())
        ],
        "events_by_entry": dict(Counter(event["entry"] for event in events)),
        "unique_sessions": len({(event["entry"], event["session"]) for event in events}),
        "http_sample_count": len(durations),
        "http_response_p95_ms": round(durations[math.ceil(.95 * len(durations)) - 1] / 1e6, 3)
        if durations else None,
    }


@app.get("/api/runtime", dependencies=[Depends(require_operator)])
def get_runtime():
    return runtime()


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_operator)])
def dashboard():
    data = runtime()
    cards = "".join(
        f'<article><h2>{title}</h2><strong>{html.escape(str(value)) if value is not None else "暂无数据"}</strong></article>'
        for title, value in (
            ("世界版本", data["world_version"]), ("事件", sum(data["events_by_entry"].values())),
            ("会话", data["unique_sessions"]), ("HTTP 响应 p95（ms）", data["http_response_p95_ms"]),
            ("Canary 签发", data["canary"]["issued"]),
            ("含 Canary 的 HTTP 响应", data["canary"]["http_response_sent"]),
            ("假 AI API 凭据命中", data["canary"]["fake_api_matches"]),
            ("MySQL 核验命中", data["canary"]["mysql_matches"]),
            ("SSH 传感器命中", data["canary"]["ssh_matches"]),
        )
    )
    services = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in service.values()) + "</tr>"
        for service in data["projected_services"]
    )
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>蜜网运行状态</title><style>
body{{font:16px system-ui;background:#eef2f5;color:#1c2938;margin:0;display:flex;min-height:100vh}}
nav{{background:#17283f;color:white;padding:2rem;min-width:9rem}}
nav a{{color:#fff;display:block;margin-block:1rem}}
main{{padding:2rem;flex:1}} section{{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr))}}
article,table{{background:white;padding:1rem;border-radius:.6rem}} h2{{font-size:1rem}}
strong{{font-size:1.8rem}} td,th{{text-align:left;padding:.5rem}}
@media(max-width:620px){{body{{display:block}} nav{{padding:1rem}}}}
</style><nav aria-label="导航"><b>蜜网管理</b><a href="#overview">总览</a><a href="#services">DWS 服务投影</a><a href="/templates">场景模板</a><a href="/publications">数据发布</a></nav>
<main><h1 id="overview">运行总览</h1><section>{cards}</section>
<p>HTTP 延迟来自最近 {data['http_sample_count']} 次请求的响应计时。</p>
<h2 id="services">DWS 服务投影</h2><table><thead><tr><th>主机</th><th>协议</th><th>版本</th></tr></thead>
<tbody>{services}</tbody></table></main></html>"""
