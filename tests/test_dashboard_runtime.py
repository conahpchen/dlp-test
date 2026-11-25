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


def test_operator_generates_reviewable_scene_from_saved_template(monkeypatch, tmp_path, demo_seed):
    import json
    import time

    from dws.world import World
    from replay.engine import Engine
    from research import control_app, template_scenes
    from research.api import current_lab
    from tests.test_generative_research import SceneModel

    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-test")
    monkeypatch.setenv("RESEARCH_AUTOMATIC_DECOYS", "0")
    monkeypatch.setenv("RESEARCH_UNIFIED_PUBLICATION", "0")
    monkeypatch.setattr(control_app, "checkpoint", lambda: (Engine(World(demo_seed)), {}))

    class ModelMissingFirstVerification(SceneModel):
        def choose(self, messages):
            result = super().choose(messages)
            if len(self.calls) == 1:
                result["nodes"][1]["lure_instruction"] = None
            if len(self.calls) == 2:
                context = json.loads(messages[-1]["content"])
                assert "lure_instruction" in context["validation_feedback"]["instruction"]
            return result

    model = ModelMissingFirstVerification()
    monkeypatch.setattr(template_scenes, "Model", lambda: model)
    dashboard = TestClient(management)
    operator = ("operator", "synthetic-test")
    template = dashboard.post("/api/templates", auth=operator, json={
        "name": "运维核验", "variant": "agent_note", "goal": "生成一份设备巡检说明",
    }).json()
    identifier, revision = template["template_id"], template["revision"]
    request = {"template_id": identifier, "revision": revision}
    status = f"/api/templates/{identifier}/revisions/{revision}/scene"
    assert dashboard.post("/api/template-scenes", json=request).status_code == 401
    assert dashboard.get(status).status_code == 401
    for _ in range(2):
        response = dashboard.post("/api/template-scenes", auth=operator, json=request)
        assert response.status_code == 202 and response.json()["status"] == "queued"
    lab = current_lab()
    with lab.connect() as db:
        assert db.execute("SELECT count(*) FROM template_scene_jobs").fetchone() == (1,)
        db.execute("UPDATE template_scene_jobs SET status='running', lease='crashed', leased_until=0")

    with TestClient(control_app.app):
        deadline = time.monotonic() + 5
        while (job := dashboard.get(status, auth=operator).json())["status"] in ("queued", "running"):
            assert time.monotonic() < deadline
            time.sleep(.05)
    assert job["status"] == "ready"
    assert lab.active() is None
    plan = lab.plan(job["run_id"])
    assert plan["template"] == {"template_id": identifier, "revision": revision}
    assert any(node["lure_instruction"] for node in plan["scene"]["nodes"])
    assert len(model.calls) == 3
    assert any(row["run_id"] == job["run_id"] and not row["active"]
               for row in dashboard.get("/api/evidence/runs", auth=operator).json())


def test_alerts_show_actual_interactions_and_keep_credentials_out(monkeypatch, tmp_path):
    from pathlib import Path

    from entries.cowrie.bridge import main as bridge

    monkeypatch.setenv("HONEYNET_ALERTS_FILE", str(tmp_path / "alerts.sqlite"))
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(tmp_path / "checkpoint.json"))
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-test")
    monkeypatch.setattr(business, "_engine", None)
    public = TestClient(business.app)
    dashboard = TestClient(management)
    operator = ("operator", "synthetic-test")
    assert public.get("/api/alerts").status_code == 404
    assert dashboard.get("/api/alerts").status_code == 401
    assert dashboard.get("/api/alerts", auth=operator).json()["alerts"] == []
    login = {"username": "ops01", "password": "temporary-wrong-value"}
    assert public.post("/api/login", json=login).status_code == 401
    response = public.post("/api/login", json=dict(login, password="ops01-password"))
    assert response.status_code == 200
    headers = {"Authorization": "Bearer " + response.json()["token"]}
    documents = public.get("/api/documents", headers=headers).json()
    assert len([item for item in documents if item["key"].startswith("KB-OPS-")]) == 18
    assert any(f.payload["path"] == "/kb/workspace/report-fields.md"
               for f in business.get_engine().world.all_current_cls("data"))

    log = Path(__file__).parent / "fixtures" / "cowrie.sample.jsonl"
    for _ in range(2):
        assert bridge(["--in", str(log), "--alerts-only"]) == 0
    ssh = dashboard.get("/api/alerts?source=ssh", auth=operator)
    assert [row["kind"] for row in ssh.json()["alerts"]] == ["login", "login_failed", "connection"]
    web = dashboard.get("/api/alerts?source=web", auth=operator)
    assert [row["kind"] for row in web.json()["alerts"]] == ["login", "login_failed"]
    assert "temporary-wrong-value" not in web.text
    assert "认证失败" in dashboard.get("/alerts?source=ssh", auth=operator).text

    overview = dashboard.get("/?days=30", auth=operator)
    assert overview.status_code == 200
    assert "告警趋势" in overview.text and "查看每日记录" in overview.text
    assert "HTTP 响应分布" in overview.text and "认证失败" in overview.text
    assert 'href="/?days=30" aria-current="true"' in overview.text
    assert dashboard.get("/?days=31", auth=operator).status_code == 422
