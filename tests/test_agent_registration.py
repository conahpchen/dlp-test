import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dws.world import World
from management.app import app as dashboard
from replay.engine import Engine
from research.api import current_lab, install_routes
from research.model_budget import ModelBudget


def test_provenance_and_strict_agent_registration(demo_seed, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-dashboard")
    lab = current_lab()
    engine = Engine(World(demo_seed))
    run = lab.create(engine.world, "agent_note")["run_id"]
    visitor_app = FastAPI()
    install_routes(visitor_app, lambda: engine, surface="visitor")
    visitor = TestClient(visitor_app, headers={"User-Agent": "synthetic-agent"})
    other = TestClient(visitor_app, headers={"User-Agent": "synthetic-agent"})
    operator = TestClient(dashboard)
    auth = ("operator", "synthetic-dashboard")

    document = visitor.get(f"/decoy/{run}/documents/0")
    assert document.status_code == 200
    endpoint = document.json()["registration"]["href"]
    assert document.json()["registration"]["optional"] is True
    assert other.get(endpoint).status_code == 404
    assert visitor.get(endpoint).json()["optional"] is True
    assert visitor.get("/api/evidence/runs").status_code == 404
    assert operator.get("/evidence").status_code == 401
    assert operator.get("/evidence", auth=auth).status_code == 200
    assert operator.get("/api/runtime", auth=auth).json()["model_budget_today"] is None
    ModelBudget(tmp_path / "model-budget.sqlite", 1000, input_rate=1_000_000, output_rate=2_000_000).reserve(5, 10)
    assert operator.get("/api/runtime", auth=auth).json()["model_budget_today"] == {
        "reserved_tokens": 15, "estimated_cost_microusd": 25,
    }
    assert operator.get("/api/evidence/runs", auth=auth).json()[0]["run_id"] == run

    claim = dict(framework="agent_sdk", model_family="openai", instance_kind="single", task="lookup")
    first = visitor.post(endpoint, json=claim)
    assert first.status_code == 200
    assert visitor.post(endpoint, json=claim).json() == first.json()
    assert visitor.post(endpoint, json=dict(claim, task="evaluation")).status_code == 409
    secret = "unvalidated-private-text"
    rejected = visitor.post(endpoint, json=dict(claim, api_key=secret))
    assert rejected.status_code == 422
    assert secret not in rejected.text
    assert visitor.post(endpoint, json=dict(claim, task=secret)).status_code == 422
    assert visitor.post(endpoint, content=b" " * 600, headers={"Content-Type": "application/json"}).status_code == 413

    detail = operator.get(f"/api/evidence/runs/{run}", auth=auth).json()
    row, = detail["registrations"]
    assert detail["timeline"][0]["kind"] == "research.delivery"
    assert "input" not in detail["timeline"][0]
    assert "src" not in detail["timeline"][0]
    assert row["self_reported"] == claim
    assert row["server_observed"]["registration_id"] == first.json()["registration_id"]
    assert row["server_observed"]["world_version"] == 1
    assert row["inferred"]["confidence"] == "low"
    assert secret not in json.dumps(detail)
    assert other.post(endpoint, json=claim).status_code == 404

    assert other.get(f"/decoy/{run}/documents/0").status_code == 200
    second = other.post(endpoint, json=claim).json()
    assert second["registration_id"] != first.json()["registration_id"]
    assert len(operator.get(f"/api/evidence/runs/{run}", auth=auth).json()["registrations"]) == 2


def test_operator_profile_counts_behavior_and_shows_linked_evidence(demo_seed, tmp_path, monkeypatch):
    from audit.flow_evidence import ingest, observe_cowrie

    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-dashboard")
    flow = tmp_path / "flows.sqlite"
    monkeypatch.setenv("HONEYNET_FLOW_STATE_FILE", str(flow))
    lab = current_lab()
    engine = Engine(World(demo_seed))
    run = lab.create(engine.world, "agent_note")["run_id"]
    visitor_app = FastAPI()
    install_routes(visitor_app, lambda: engine, surface="visitor")
    visitor = TestClient(visitor_app, headers={"User-Agent": "synthetic-agent"}, client=("192.0.2.7", 42313))

    document = visitor.get(f"/decoy/{run}/documents/0").json()
    assert visitor.get(f"/decoy/{run}/documents/0").status_code == 200
    other = TestClient(visitor_app, client=("192.0.2.7", 42313))
    assert other.get(f"/decoy/{run}/documents/1").status_code == 200
    token = document["verification_link"].rsplit('/', 1)[-1]
    session = visitor.cookies.get("research_session")
    assert lab.observe_lure_use(token, "ssh", "cowrie/ssh-test")
    key = "synthetic-test-flow-key-for-matching"
    interface = "eni-abcdef98"
    assert observe_cowrie(flow, key, interface, "cowrie", {
        "session": "ssh-test", "src_ip": "192.0.2.7", "dst_ip": "192.0.2.10", "src_port": 42313,
        "dst_port": 2222, "timestamp": "1970-01-01T00:01:41+00:00",
    })
    ingest(flow, key, [f"2 123456789012 {interface} 192.0.2.7 192.0.2.10 42313 2222 6 5 400 100 102 ACCEPT OK"])

    operator = TestClient(dashboard)
    auth = ("operator", "synthetic-dashboard")
    assert visitor.get("/profiles").status_code == 404
    assert operator.get("/profiles").status_code == 401
    params = {"run": run, "session": session}
    web = operator.get("/profiles", params=params, auth=auth)
    assert web.status_code == 200
    assert "行为统计" in web.text and "192.0.2.0/24" in web.text
    assert "Agent 参与研判" in web.text and "Agent 证据不足" in web.text
    assert "请求头出现 Agent 相关标记" in web.text
    assert "疑似 AI Agent 参与" not in web.text
    assert "<td>资料交付记录</td><td>2</td>" in web.text
    assert "<td>/kb/handbook.md</td><td>2</td>" in web.text and "/kb/policy.md" not in web.text
    assert "cowrie/ssh-test" in web.text and "已关联" in web.text
    assert token not in web.text and "192.0.2.7" not in web.text
    evidence = operator.get(f"/api/evidence/runs/{run}", auth=auth).raise_for_status().json()
    events = {item["session"]: item["event_id"] for item in evidence["timeline"]}
    other_session = other.cookies.get("research_session")

    def save_judgment(event, hypothesis="ai_agent_suspected", status="applied"):
        record = {"session": session, "status": status,
                  "decision": {"actor_hypothesis": hypothesis, "evidence_event_ids": [event]}}
        with lab.connect() as db:
            db.execute("INSERT INTO decisions(run,session,payload) VALUES (?,?,?)",
                       (run, session, json.dumps(record)))

    save_judgment(events[other_session])
    different_evidence = operator.get("/profiles", params=params, auth=auth)
    assert "Agent 证据不足" in different_evidence.text
    save_judgment(events[session], status="rejected")
    rejected = operator.get("/profiles", params=params, auth=auth)
    assert "Agent 证据不足" in rejected.text
    save_judgment(events[session])
    assessed = operator.get("/profiles", params=params, auth=auth)
    assert "疑似 AI Agent 参与" in assessed.text and "置信度低" in assessed.text
    assert f'#event-{events[session]}' in assessed.text
    untouched = operator.get("/profiles", params={**params, "session": other_session}, auth=auth)
    assert "Agent 证据不足" in untouched.text and "疑似 AI Agent 参与" not in untouched.text

    ssh = operator.get("/profiles", params={**params, "session": "cowrie/ssh-test"}, auth=auth)
    assert ssh.status_code == 200 and interface in ssh.text
    assert "最近行为记录" in ssh.text and "<td>Canary 命中</td>" in ssh.text
    assert "<td>5</td><td>400</td>" in ssh.text
    assert "1970-01-01 00:01:40" in ssh.text
    assert "/kb/handbook.md" not in ssh.text and "疑似 AI Agent" not in ssh.text
    save_judgment(events[session], hypothesis="unknown")
    latest = operator.get("/profiles", params=params, auth=auth)
    assert "Agent 证据不足" in latest.text and "疑似 AI Agent 参与" not in latest.text
    assert operator.get("/profiles", params={**params, "session": "missing"}, auth=auth).status_code == 200
