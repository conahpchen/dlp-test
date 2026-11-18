from fastapi.testclient import TestClient

from business import app as business
from management.app import app as management


def test_management_listener_displays_measured_public_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("HONEYNET_LATENCY_FILE", str(tmp_path / "latency.sqlite"))
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(tmp_path / "checkpoint.json"))
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-test")
    monkeypatch.setattr(business, "_engine", None)
    public = TestClient(business.app)
    dashboard = TestClient(management)
    assert public.get("/api/runtime").status_code == 404
    assert public.get("/v1/models").status_code == 200
    assert dashboard.get("/api/runtime").status_code == 401
    data = dashboard.get("/api/runtime", auth=("operator", "synthetic-test")).json()
    assert data["http_sample_count"] == 2
    assert data["http_response_p95_ms"] > 0
    assert data["world_version"] == 1
    assert data["projected_services"]
    assert dashboard.get("/", auth=("operator", "synthetic-test")).status_code == 200


def test_templates_are_authenticated_revisioned_and_used_in_runs(monkeypatch, tmp_path, demo_seed):
    from fastapi import FastAPI

    from dws.world import World
    from replay.engine import Engine
    from research.api import current_lab, install_routes

    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-test")
    monkeypatch.setenv("HONEYNET_CONTROL_TOKEN", "control-test")
    operator = ("operator", "synthetic-test")
    dashboard = TestClient(management)
    initial = {"name": "数据库目录", "variant": "agent_note", "goal": "生成数据库备份场景"}
    assert dashboard.post("/api/templates", json=initial).status_code == 401
    assert dashboard.get("/templates", auth=operator).status_code == 200
    assert dashboard.get("/api/templates", auth=operator).json()[0]["template_id"] == "operations"
    created = dashboard.post("/api/templates", auth=operator, json=initial).json()
    identifier = created["template_id"]
    edit = dict(initial, template_id=identifier, expected_revision=1, variant="plain")
    assert dashboard.post("/api/templates", auth=operator, json=edit).json()["revision"] == 2
    assert dashboard.post("/api/templates", auth=operator, json=edit).status_code == 409

    control = FastAPI()
    engine = Engine(World(demo_seed))
    install_routes(control, lambda: engine, surface="control")
    headers = {"x-control-token": "control-test"}
    result = TestClient(control).post("/api/research/runs", headers=headers,
                                      json={"mode": "fixture", "template_id": identifier, "template_revision": 1})
    assert result.status_code == 200
    plan = current_lab().plan(result.json()["run_id"])
    assert plan["variant"] == "agent_note"
    assert plan["template"] == {"template_id": identifier, "revision": 1}
