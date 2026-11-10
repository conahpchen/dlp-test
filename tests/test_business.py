"""协作平台（MVP）：登录/工单/资产/审计一致性/三来源采集（契约 12 §13.3、14 §04）。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from business import app as biz_app


@pytest.fixture()
def client(demo_seed, monkeypatch):
    """每测试一个干净业务实例（bundled 种子，禁用 PG）。"""
    monkeypatch.setenv(
        "GATEWAY_WORLD_SEED",
        str(biz_app.REPO / "tests" / "fixtures" / "world_seed.demo.json"),
    )
    monkeypatch.delenv("HONEYNET_PG_DSN", raising=False)
    monkeypatch.setattr(biz_app, "_engine", None)
    biz_app._sessions.clear()
    biz_app._audit_rows.clear()
    biz_app._events.clear()
    biz_app._event_ids.clear()
    monkeypatch.setenv("HONEYNET_CONTROL_TOKEN", "test-control")
    return TestClient(biz_app.app, headers={"x-control-token": "test-control"})


def _login(client, u="ops01", p="ops01-password"):
    r = client.post("/api/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_ok_and_fail(client):
    r = client.post(
        "/api/login", json={"username": "ops01", "password": "ops01-password"}
    )
    assert r.status_code == 200
    body = r.json()
    assert "token" in body and body["role"] in (
        "sysadmin",
        "tester",
        "biz",
        "manager",
        "dba",
    )
    r = client.post("/api/login", json={"username": "ops01", "password": "wrong"})
    assert r.status_code == 401


def test_unauthenticated_rejected(client):
    for path in ("/api/tickets", "/api/assets", "/api/audit"):
        assert client.get(path).status_code == 401


def test_ticket_lifecycle_with_correlation(client):
    token = _login(client)
    r = client.post(
        "/api/tickets",
        headers=_h(token),
        json={"title": "应用接口异常", "description": "接口返回 500"},
    )
    assert r.status_code == 200
    key = r.json()["key"]
    assert key == "HD-0001"

    # correlation 全程可追溯
    events = client.get("/api/events", headers=_h(token)).json()["events"]
    biz_events = [e for e in events if e.get("what", {}).get("kind") == "web.request"]
    assert biz_events and all(e["correlation_id"] == key for e in biz_events)

    # 非法状态迁移被拒
    r = client.post(
        f"/api/tickets/{key}/status", headers=_h(token), json={"status": "open"}
    )
    assert r.status_code == 400

    # 合法迁移
    r = client.post(
        f"/api/tickets/{key}/status", headers=_h(token), json={"status": "in_progress"}
    )
    assert r.json()["status"] == "in_progress"


def test_assets_projection_matches_dws(client, demo_seed):
    token = _login(client)
    r = client.get("/api/assets", headers=_h(token))
    hostnames = {h["hostname"] for h in r.json()["hosts"]}
    assert {"web01", "db01", "ai01", "oa01"} <= hostnames


def test_audit_format_identical_across_users(client):
    """审计一致性（契约 12 §13.3）：不同用户（含 Agent 借用账号）审计行格式一致，
    业务审计层不存在任何 agent 标记。"""
    t1 = _login(client, "ops01", "ops01-password")
    t2 = _login(client, "dev01", "dev01-password")
    client.get("/api/tickets", headers=_h(t1))
    client.get("/api/tickets", headers=_h(t2))

    rows = client.get("/api/audit", headers=_h(t2)).json()["rows"]
    assert len(rows) >= 4
    keys = {tuple(sorted(r.keys())) for r in rows}
    assert len(keys) == 1  # 所有行同一结构
    joined = json.dumps(rows, ensure_ascii=False)
    assert "agent" not in joined.lower()  # 无 agent 标记泄漏进业务审计


def test_three_source_coverage(client, tmp_path):
    """三类来源统一采集：攻击者（ssh）/ Agent / 业务（web）。"""
    token = _login(client)
    # ① 攻击者事件：桥接转换样例后 ingest
    from entries.cowrie.bridge import main as bridge_main

    src = biz_app.REPO / "tests" / "fixtures" / "cowrie.sample.jsonl"
    out = tmp_path / "events.jsonl"
    bridge_main(["--in", str(src), "--out", str(out)])
    client.post("/api/events/ingest", content=out.read_text(encoding="utf-8"))
    # ② Agent 事件：ToolAdapter 调用产生（entry=agent）
    from agent_runtime.adapter import ToolAdapter

    a = ToolAdapter(
        base_url="http://test",
        username="dev01",
        password="dev01-password",
        allowlist=["ticket_list"],
        agent_id="agent-dev-01",
        workstation_ip="10.20.3.32",
        client=client,
    )
    a.login()
    a.call("ticket_list")
    for ev in a.events:
        client.post("/api/events/ingest", content=json.dumps(ev, ensure_ascii=False))
    # ③ 业务事件：登录/工单自动产生
    client.post(
        "/api/tickets",
        headers=_h(token),
        json={"title": "三来源验证", "description": ""},
    )

    events = client.get("/api/events", headers=_h(token)).json()["events"]
    entries = {e["entry"] for e in events}
    assert {"ssh", "agent", "web"} <= entries
