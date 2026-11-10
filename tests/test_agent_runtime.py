"""Agent 工位机运行时 + 工单流转演示（契约 14 §07.1a/§07.2/§04）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_runtime.runtime import TRANSITIONS, WorkstationAgent, in_window, to_minute
from agent_runtime.workflow import run_demo_workflow
from business import app as biz_app
from contracts.agent_personas import load_personas
from replay.harness import validate_events


@pytest.fixture()
def client(demo_seed, monkeypatch):
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


def _password(username: str) -> str:
    return f"{username}-password"


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_state_machine_transitions_legal(client):
    """7 状态全部可达且所有迁移合法（R_t = 100%）。"""
    personas = {p["agent_id"]: p for p in load_personas()}
    p = personas["agent-ops-01"]

    class FakeAdapter:
        allowlist = set(p["tools"])
        events: list = []

        def call(self, tool, correlation_id=None, **kw):
            # ticket_list 返回非空列表（有工单才有 WORKING 的任务上下文）
            if tool == "ticket_list":
                return {"status": 200, "body": [{"key": "HD-0001", "status": "open"}]}
            return {"status": 200, "body": []}

    a = WorkstationAgent(p, FakeAdapter(), seed="t1")
    seen = set()
    for minute in range(570, 1080, 7):  # 09:30-18:00 窗口内快进
        for e in a.step(minute):
            if e["what"]["kind"] == "agent.state":
                seen.add(e["what"]["state"])
    assert seen <= set(TRANSITIONS)
    assert {"IDLE", "CHECKING", "WORKING", "REPORTING"} <= seen


def test_schedule_window_enforced():
    p = {
        "agent_id": "a1",
        "username": "u",
        "workstation_ip": "10.20.3.31",
        "schedule": {"windows": [["09:00", "18:00"]]},
        "tools": ["ticket_list"],
        "state_tasks": {"CHECKING": ["ticket_list"]},
    }

    class FakeAdapter:
        allowlist = set(p["tools"])
        events: list = []

        def call(self, tool, correlation_id=None, **kw):
            return {"status": 200, "body": []}

    a = WorkstationAgent(p, FakeAdapter())
    assert in_window(a.windows, to_minute("09:00"))
    assert in_window(a.windows, to_minute("12:30"))
    assert not in_window(a.windows, to_minute("03:00"))
    # 窗外 → OFFLINE
    a.step(to_minute("03:00"))
    assert a.state == "OFFLINE"


def test_workflow_demo_passes(client):
    report = run_demo_workflow("http://test", client=client)
    assert report["correlation_ok"] is True
    assert report["ticket"]["status"] == "closed"
    assert len(report["steps"]) == 7


def test_workflow_agent_events_schema_valid(client):
    report = run_demo_workflow("http://test", client=client)
    assert report["correlation_ok"] is True
    tok = client.post(
        "/api/login", json={"username": "manager01", "password": _password("manager01")}
    ).json()["token"]
    agent_events = [
        e
        for e in client.get("/api/events", headers=_h(tok)).json()["events"]
        if e["entry"] == "agent"
    ]
    assert agent_events, "工作流应产生 Agent 事件"
    validate_events(agent_events)  # 不抛即 schema 合法
    assert all(e["what"]["kind"].startswith("agent.") for e in agent_events)


def test_tool_adapter_allowlist_enforced(client):
    """allowlist 之外的工具调用必须被拒（最小授权）。"""
    from agent_runtime.adapter import ToolAdapter, ToolDenied

    personas = {p["agent_id"]: p for p in load_personas()}
    p = personas["agent-ops-01"]  # ops 无 ticket_status 工具
    a = ToolAdapter(
        base_url="http://test",
        username=p["username"],
        password=_password(p["username"]),
        allowlist=p["tools"],
        agent_id=p["agent_id"],
        workstation_ip=p["workstation_ip"],
        client=client,
    )
    a.login()
    with pytest.raises(ToolDenied):
        a.call("ticket_status", key="HD-0001", body={"status": "closed"})
