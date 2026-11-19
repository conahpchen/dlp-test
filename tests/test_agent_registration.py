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
