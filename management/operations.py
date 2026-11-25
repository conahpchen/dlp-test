"""Read-only deployment status, DWS changes and observed request metrics."""

import math
import os
import socket
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from audit.employee_activity import recent_activity
from audit.http_latency import samples
from business.atomic_state import read
from business.storage import location
from management.layout import esc, metric, page, table, table_tools
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
    with lab.connect(write=False) as db:
        publications = dict(db.execute("SELECT status, count(*) FROM generated_releases GROUP BY status"))
        retrying = db.execute("SELECT COUNT(*) FROM publication_retry r JOIN generated_releases g ON g.id=r.job "
                              "WHERE r.lane='generated' AND g.status='pending'").fetchone()[0]
    return {"listeners": listeners, "active_run": active, "release": lab.release_status(active) if active else None,
            "generated_publications": {"pending": publications.get("pending", 0), "retrying": retrying,
                                       "failed": publications.get("failed", 0)},
            "employee_activity": recent_activity(os.environ.get("HONEYNET_EMPLOYEE_ACTIVITY_FILE")),
            "release_health": lab.compensate_unready(inspect_only=True) if active else None, "latency_windows": _latency_windows(), "world_changes": _changes(), "model_budget_today": usage_today()}


def operations_router(require_operator):
    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/api/operations")
    def data():
        return status()

    @router.get("/operations", response_class=HTMLResponse)
    def show():
        snapshot = status()
        labels = {"unconfigured": "未配置", "unreachable": "不可达",
                  "unexpected": "响应异常", "responding": "有响应"}
        tones = {"responding": "ok", "unreachable": "bad", "unexpected": "warn", "unconfigured": ""}
        listeners = snapshot["listeners"]
        budget = snapshot["model_budget_today"] or {}
        publications = snapshot["generated_publications"]
        health = (snapshot["release_health"] or {}).get("status")
        release_names = {"healthy": "依赖健康", "unready": "依赖异常",
                         "not_applicable": "暂无发布清单"}
        services = {"ssh": "SSH", "mysql": "MySQL", "llm": "模拟 AI API", "web": "OA 工单"}
        employee_status = {"completed": "已完成", "failed": "交互失败", "unconfigured": "未配置凭据"}
        tasks = {"check_host": "主机巡检", "review_deploy_notes": "查看发布资料",
                 "review_catalog": "复核库目录", "check_readiness": "连通性检查",
                 "read_handbook": "查看运维手册", "review_service_inventory": "复核服务资产",
                 "review_ticket_queue": "检查工单队列"}
        manifest = (snapshot["release"] or {}).get("manifest") or {}
        cards = "".join((
            metric("监听器有响应", f'{sum(r["status"] == "responding" for r in listeners)} / {len(listeners)}',
                   "按本次回环探测", "mint"),
            metric("当前发布依赖", release_names.get(health), "按已部署能力检查"),
            metric("今日已计入 Token", budget.get("reserved_tokens"), "控制面模型预算"),
            metric("今日估算费用", budget.get("estimated_cost_microusd"), "单位为微美元", "warm"),
        ))
        inventory = "".join(f"""
            <div class="record"><h3>{esc(r["name"])}</h3>
            <p>{esc(r["protocol"])} · 回环端口 {esc(r["port"] or "—")}</p>
            <p><span class="tag {tones[r["status"]]}">{labels[r["status"]]}</span></p></div>""" for r in listeners)
        load = snapshot["latency_windows"]
        peak = max((w["p95_ms"] for w in load), default=0)
        chart = "".join(f'<div class="column"><div class="bar" style="--height:{w["p95_ms"] * 100 / peak if peak else 0:g}%" '
                        f'title="样本 {w["first_sample"]} · {w["p95_ms"]} ms"></div></div>' for w in load)
        version = manifest.get("world_version")
        service_table = table(("能力", "分级", "协议", "可达范围", "绑定版本"),
            ((c["name"], f'L{c["level"]}', c["protocol"], c["exposure"],
              f"v{version}" if version is not None else "—") for c in manifest.get("capabilities", ())),
            "当前场景没有发布能力清单")
        employee_table = table(("时间（UTC）", "员工", "入口", "任务", "结果"),
            ((r["recorded_at"], r["username"], services.get(r["service"], r["service"]),
              tasks.get(r["task"], r["task"]), employee_status.get(r["status"], r["status"]))
             for r in snapshot["employee_activity"]), "尚未收到员工 Agent 活动")
        latency = table(("首样本", "请求数", "服务错误", "实测 p95（ms）"),
                        ((w["first_sample"], w["requests"], w["server_errors"], w["p95_ms"]) for w in load),
                        "尚未采样到 HTTP 请求")
        body = f"""<section class="metrics">{cards}</section>
        <section class="panel"><div class="panel-header"><div><h2>蜜罐服务</h2><p>握手或 HTTP 响应基于实际探测</p></div>
        <a href="/world">DWS 服务与关系 →</a></div><div class="service-overview">{inventory}</div></section>
        <div class="dashboard-grid"><section class="panel"><div class="panel-header"><div><h2>HTTP 响应延迟</h2>
        <p>每组最多 64 个请求，p95 按单请求样本计算。</p></div></div>
        {f'<div class="chart" aria-hidden="true">{chart}</div>' if chart else ""}{latency}</section>
        <section class="panel"><div class="panel-header"><div><h2>当前投放</h2>
        <p class="mono">{esc(snapshot["active_run"] or "暂无活跃运行")}</p></div><a href="/releases">发布记录 →</a></div>
        <p>AI 生成待发布 {publications["pending"]} · 重试中 {publications["retrying"]} · 失败 {publications["failed"]}</p>
        {service_table}</section></div>
        <section class="panel"><div class="panel-header"><div><h2>员工 Agent 跨服务活动</h2>
        <p>记录按真实协议执行的日常任务及结果。</p></div></div>
        {table_tools("employee-rows", "搜索员工、协议或任务")}<div id="employee-rows">{employee_table}</div></section>"""
        return page("/operations", "部署与运行", "检查入口健康、发布依赖、实测延迟、模型用量和员工活动。", body,
                    actions='<a class="button" href="/operations">重新探测</a>')

    return router
