"""Read-only deployment status, DWS changes and observed request metrics."""

import html
import math
import os
import socket
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from audit.http_latency import samples
from business.atomic_state import read
from business.storage import location
from research.api import current_lab
from research.model_budget import usage_today


def _port(name):
    raw = os.environ.get(name, "")
    return int(raw) if raw.isascii() and raw.isdecimal() and 1 <= int(raw) <= 65535 else None


def _probe(name, env, protocol, path=None):
    port = _port(env)
    result = {"name": name, "protocol": protocol, "port": port,
              "status": "unconfigured" if port is None else "unreachable"}
    if port is None:
        return result
    try:
        if path:
            with httpx.Client(timeout=1, trust_env=False) as client:
                response = client.get(f"http://127.0.0.1:{port}{path}")
                # An authentication challenge proves the intended isolated listener is responding.
                result["status"] = "responding" if response.status_code in (200, 401) else "unexpected"
        else:
            with socket.create_connection(("127.0.0.1", port), timeout=1) as connection:
                prefix = connection.recv(16, socket.MSG_WAITALL)
                valid = prefix.startswith(b"SSH-") if protocol == "SSH" else len(prefix) >= 5 and prefix[4] == 10
                result["status"] = "responding" if valid else "unexpected"
    except (OSError, httpx.HTTPError):
        pass
    return result


def _latency_windows():
    rows = samples()
    windows = []
    for start in range(0, len(rows), 64):
        group = rows[start:start + 64]
        durations = sorted(duration for _, duration in group)
        windows.append({
            "first_sample": start + 1, "requests": len(group),
            "server_errors": sum(status >= 500 for status, _ in group),
            "p95_ms": round(durations[math.ceil(.95 * len(group)) - 1] / 1e6, 3),
        })
    return windows[-16:]


def _changes():
    checkpoint = location()
    if not checkpoint or not checkpoint.is_file():
        return []
    state = read(checkpoint)
    private = set(state.get("private_versions", ()))
    initial = min((row["world_version"] for entities in state["facts"].values()
                   for versions in entities.values() for row in versions[:1]), default=1)
    changes = []
    for kind in ("host", "identity", "data", "relation", "object"):
        for versions in state["facts"].get(kind, {}).values():
            for row in versions:
                if row["world_version"] > initial and row["world_version"] not in private:
                    changes.append({"kind": kind, "entity": row["entity_id"],
                                    "world_version": row["world_version"], "revision": row["version"]})
    return sorted(changes, key=lambda row: row["world_version"], reverse=True)[:20]


def status():
    probes = [
        ("公开 Web 与假 AI", "HONEYNET_VISITOR_PORT", "HTTP", "/healthz"),
        ("Cowrie", "HONEYNET_COWRIE_PORT", "SSH", None),
        ("MySQL 目录", "HONEYNET_MYSQL_PORT", "MySQL", None),
        ("管理端", "HONEYNET_DASHBOARD_PORT", "HTTP", "/"),
    ]
    with ThreadPoolExecutor(max_workers=len(probes)) as threads:
        listeners = list(threads.map(lambda args: _probe(*args), probes))
    lab = current_lab()
    active = lab.active()
    return {"listeners": listeners, "active_run": active, "release": lab.release_status(active) if active else None,
            "release_health": lab.compensate_unready(inspect_only=True) if active else None, "latency_windows": _latency_windows(), "world_changes": _changes(), "model_budget_today": usage_today()}


def operations_router(require_operator):
    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/api/operations")
    def data():
        return status()

    @router.get("/operations", response_class=HTMLResponse)
    def page():
        snapshot = status()

        def table(columns, rows):
            header = "".join("<th>" + html.escape(column) + "</th>" for column in columns)
            body = "".join("<tr>" + "".join("<td>" + html.escape(str(value)) + "</td>" for value in row)
                           + "</tr>" for row in rows)
            return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>" if body else "<p>暂无记录</p>"

        names = {"unconfigured": "未配置", "unreachable": "不可达", "unexpected": "响应异常", "responding": "有响应"}
        details = snapshot["release"] or {}
        manifest = details.get("manifest") or {}
        budget = snapshot["model_budget_today"]
        health = (snapshot["release_health"] or {}).get("status")
        release_health = {"healthy": "已通过能力检查", "unready": "有依赖异常", "not_applicable": "暂无发布清单"}

        return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>部署与运行 · 蜜网管理</title>
<style>body{{margin:0;background:#eef2f5;color:#192839;font:16px system-ui}}header{{background:#17283f;color:white;padding:1.25rem}}
header a{{color:white;margin-right:1rem}}main{{max-width:72rem;margin:auto;padding:1.5rem}}section{{background:white;padding:1.1rem;border-radius:.6rem;margin:1rem 0;overflow:auto}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:.5rem;text-align:left;border-bottom:1px solid #e4e9ee}}h2{{margin-top:0}}</style>
<header><a href="/">运行总览</a><a href="/evidence">会话与发布</a><a href="/publications">数据发布</a></header>
<main><h1>部署与运行</h1><a href="/operations">刷新</a>
<section><h2>入站监听器</h2>{table(("入口", "协议", "回环端口", "探测结果"), ((r["name"], r["protocol"], r["port"] or "—", names[r["status"]]) for r in snapshot["listeners"]))}</section>
<section><h2>当前投放</h2><p>{html.escape(snapshot["active_run"] or "暂无活跃运行")}</p>
<p>{release_health.get(health, "暂无检查结果")}</p>
{table(("能力", "分级", "协议", "可达范围"), ((c["name"], c["level"], c["protocol"], c["exposure"]) for c in manifest.get("capabilities", [])))}</section>
<section><h2>最近 HTTP 请求窗口</h2>{table(("窗口首个样本", "请求数", "服务错误数", "p95 毫秒"), (tuple(r.values()) for r in snapshot["latency_windows"]))}</section>
<section><h2>DWS 变更</h2>{table(("实体类型", "实体 ID", "DWS 版本", "实体修订"), (tuple(r.values()) for r in snapshot["world_changes"]))}</section>
<section><h2>今日模型账本</h2>{table(("预留 token", "估算费用（微美元）"), (tuple(budget.values()),) if budget else ())}</section>
</main></html>"""

    return router
