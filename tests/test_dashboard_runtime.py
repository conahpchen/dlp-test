import httpx
import pytest
from fastapi.testclient import TestClient

from business import app as business
from management.app import app as management
from research.model import ModelProviderError, ModelResponseError


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

    from business.enterprise import enrich_seed
    from dws.world import World
    from replay.engine import Engine
    from research import control_app, template_scenes
    from research.api import current_lab
    from tests.test_generative_research import SceneModel

    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-test")
    monkeypatch.setenv("RESEARCH_AUTOMATIC_DECOYS", "0")
    monkeypatch.setenv("RESEARCH_UNIFIED_PUBLICATION", "1")
    monkeypatch.setattr(control_app, "checkpoint", lambda: (Engine(World(enrich_seed(demo_seed))), {}))

    class ModelAuthorsOnlyBusinessContent(SceneModel):
        def choose(self, messages):
            result = super().choose(messages)
            if len(self.calls) == 1:
                result["type"] = "json_object"
                result["nodes"][0]["body"] = "巡检对象为 [[dws:a-label]]。"
                result["nodes"][1]["lure_instruction"] = None
            if len(self.calls) == 2:
                context = json.loads(messages[-1]["content"])
                assert "entity_graph" not in context
                assert len(context["sources"]) <= 2
                assert any(page.get("lure_instruction") for page in context["pages"])
            return result

    model = ModelAuthorsOnlyBusinessContent()
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
    assert len(model.calls) == 2
    assert len(plan["scene"]["entity_links"]) == 2
    draft = json.loads(model.calls[0][-1]["content"])
    assert "use services" not in model.calls[0][0]["content"]
    assert "entity_graph" not in draft and len(draft["relationship"]) == 3
    assert all(len(source["content"]) <= 360 for source in draft["sources"])
    claim, = plan["scene"]["fact_claims"]
    assert claim["id"] == "a-label"
    assert claim["value"] in {entity["label"] for entity in plan["entity_graph"]["entities"]}
    assert draft["facts"]["[[dws:a-label]]"][1] == "label"
    assert plan["scene"]["nodes"][0]["sources"][0]["fact_id"] in {doc["fact_id"] for doc in plan["documents"]}
    assert any(row["run_id"] == job["run_id"] and not row["active"]
               for row in dashboard.get("/api/evidence/runs", auth=operator).json())


@pytest.mark.parametrize(("failure", "code"), [
    (httpx.ReadTimeout("provider details"), "model_timeout"),
    (ModelProviderError(401), "model_auth"),
    (ModelProviderError(429), "model_rate_limit"),
    (ModelResponseError("malformed provider content"), "model_response_invalid"),
    (ValueError("Use structured DWS claims for infrastructure facts"), "scene_dws_validation"),
    (ValueError("Independent model review rejected scene"), "scene_review_rejected"),
])
def test_scene_generation_reports_safe_failure_and_allows_retry(monkeypatch, tmp_path, demo_seed, failure, code):
    from dws.world import World
    from research import template_scenes
    from research.api import current_lab
    from tests.test_generative_research import SceneModel

    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("RESEARCH_UNIFIED_PUBLICATION", "0")
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-test")
    dashboard = TestClient(management)
    operator = ("operator", "synthetic-test")
    request = {"template_id": "operations", "revision": 1}
    status_path = "/api/templates/operations/revisions/1/scene"

    class FailingModel(SceneModel):
        def choose(self, messages):
            raise failure

    lab = current_lab()
    monkeypatch.setattr(template_scenes, "Model", FailingModel)
    queued = dashboard.post("/api/template-scenes", auth=operator, json=request).json()
    assert template_scenes.process_one(lab, lambda: World(demo_seed))
    response = dashboard.get(status_path, auth=operator)
    result = response.json()
    assert result["status"] == "failed" and result["run_id"] is None
    assert result["job_id"] == queued["job_id"] and result["error"] == code
    assert result["stage"] == "preparing" and result["elapsed_seconds"] >= 0
    assert str(failure) not in response.text
    with lab.connect(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)

    monkeypatch.setattr(template_scenes, "Model", SceneModel)
    retry = dashboard.post("/api/template-scenes", auth=operator, json=request).json()
    assert retry["job_id"] != queued["job_id"]
    assert template_scenes.process_one(lab, lambda: World(demo_seed))
    ready = dashboard.get(status_path, auth=operator).json()
    assert ready["status"] == "ready" and ready["run_id"] == retry["job_id"]


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


@pytest.mark.parametrize(("cowrie_host", "include_required_assets", "expected_error", "calls"), [
    ("web01", True, None, 2),
    ("web01", False, "scene_scope_incomplete", 1),
    ("missing-host", True, "world_scope_missing_ssh", 0),
])
def test_template_queue_preflights_cross_entry_grounding(
    monkeypatch, tmp_path, demo_seed, cowrie_host, include_required_assets, expected_error, calls
):
    import json

    from business.enterprise import enrich_seed
    from dws.world import World
    from research import artifacts, template_scenes
    from research.api import current_lab
    from tests.test_generative_research import SceneModel

    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-test")
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("RESEARCH_UNIFIED_PUBLICATION", "1")
    monkeypatch.setenv("HONEYNET_COWRIE_HOST", cowrie_host)
    world = World(enrich_seed(demo_seed))

    class CrossEntryModel(SceneModel):
        def choose(self, messages):
            result = super().choose(messages)
            context = json.loads(messages[-1]["content"])
            if len(self.calls) == 1:
                assert {"ssh", "mysql"} == context["entry_assets"].keys()
                assert {edge[1] for edge in context["relationships"]} == {"hosted_on", "l3-reachable"}
                assert "entity_graph" not in context and "collab_core" not in context["facts"].values()
                if include_required_assets:
                    result["nodes"][0]["body"] = (
                        "SSH 资料来自 " + context["entry_assets"]["ssh"] +
                        "。模拟目录对应 " + context["entry_assets"]["mysql"] + "。"
                    )
            return result

    model = CrossEntryModel()
    monkeypatch.setattr(template_scenes, "Model", lambda: model)
    dashboard = TestClient(management)
    operator = ("operator", "synthetic-test")
    template = dashboard.post("/api/templates", auth=operator, json={
        "name": "跨入口目录核验", "variant": "plain",
        "goal": "让 SSH 日志和 MySQL 目录引用同一个资产 collab_core。",
    }).raise_for_status().json()
    request = {key: template[key] for key in ("template_id", "revision")}
    dashboard.post("/api/template-scenes", auth=operator, json=request).raise_for_status()
    lab = current_lab()
    assert template_scenes.process_one(lab, lambda: world)
    status_path = f"/api/templates/{template['template_id']}/revisions/{template['revision']}/scene"
    job = dashboard.get(status_path, auth=operator).raise_for_status().json()
    assert job["error"] == expected_error
    assert len(model.calls) == calls
    if expected_error:
        assert job["status"] == "failed" and job["run_id"] is None
    else:
        plan = lab.plan(job["run_id"])
        assert len(plan["entity_graph"]["entities"]) == 3
        graph = artifacts._scene_graph(world, plan, plan["scene"])
        assert {edge["kind"] for edge in graph["edges"]} >= {"hosted_on", "l3-reachable"}
        assert any("web01" in node.get("content", "") and "collab_core" in node.get("content", "")
                   for node in graph["nodes"])
