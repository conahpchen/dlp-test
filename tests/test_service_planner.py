import asyncio
import json

import pytest

from business.enterprise import enrich_seed
from business.storage import persist
from dws.world import World
from entries.mysql.app import create_server
from replay.engine import Engine
from research.lab import ResearchLab
from tests.test_generative_research import SceneModel


def add_capability(lab, run, capability):
    plan = lab.plan(run)
    plan["scene"]["capability_requests"] = [capability]
    with lab.connect() as db:
        db.execute("UPDATE runs SET plan=? WHERE id=?", (json.dumps(plan), run))


def test_release_requires_real_mysql_readiness_and_preserves_bound_visitors(tmp_path, monkeypatch, demo_seed):
    checkpoint = tmp_path / "world.sqlite"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(checkpoint))
    monkeypatch.delenv("HONEYNET_MYSQL_HEALTH_PASSWORD", raising=False)
    engine = Engine(World(enrich_seed(demo_seed)))
    persist(engine, [], [])
    lab = ResearchLab(tmp_path / "research.sqlite")
    first = lab.create(engine.world, "plain", model=SceneModel())["run_id"]
    next_run = lab.create(engine.world, "plain", model=SceneModel())["run_id"]
    unsupported = lab.create(engine.world, "plain", model=SceneModel())["run_id"]
    add_capability(lab, next_run, "mysql_catalog")
    add_capability(lab, unsupported, "isolated_instance")
    assert lab.activate(first)["active_run"] == first
    assert lab.active_for("old") == first
    manifest = lab.release_status(first)["manifest"]
    assert [item["level"] for item in manifest["capabilities"]] == [0, 1]
    assert manifest["new_processes"] == 0
    with pytest.raises(ValueError, match="isolated runtime"):
        lab.activate(unsupported)
    with pytest.raises(ValueError, match="protocol readiness"):
        lab.activate(next_run)
    assert lab.active() == first
    assert lab.release_status(next_run)["attempts"][0]["status"] == "rejected"

    async def check():
        mysql = create_server(checkpoint, "synthetic-password")
        await mysql.start_server(host="127.0.0.1", port=0)
        try:
            port = mysql.sockets()[0].getsockname()[1]
            monkeypatch.setenv("HONEYNET_MYSQL_PORT", str(port))
            monkeypatch.setenv("HONEYNET_MYSQL_HEALTH_PASSWORD", "wrong-password")
            with pytest.raises(ValueError, match="readiness"):
                await asyncio.to_thread(lab.activate, next_run)
            assert lab.active() == first
            monkeypatch.setenv("HONEYNET_MYSQL_HEALTH_PASSWORD", "synthetic-password")
            assert (await asyncio.to_thread(lab.activate, next_run))["active_run"] == next_run
        finally:
            mysql.close()
            await mysql.wait_closed()

    asyncio.run(check())
    reopened = ResearchLab(lab.path)
    status = reopened.release_status(next_run)
    assert status["active"]
    assert status["manifest"]["capabilities"][-1] == {
        "name": "mysql_catalog", "level": 2, "protocol": "mysql",
        "exposure": "configured_loopback", "risk": "moderate",
    }
    assert reopened.active_for("old") == first
    assert reopened.active_for("new") == next_run
    assert reopened.compensate_unready() == {"status": "compensated", "active_run": first}
    assert reopened.active_for("new") == next_run
    assert reopened.active_for("later") == first
    assert reopened.release_status(next_run)["attempts"][0]["status"] == "compensated"


def test_dashboard_activates_and_serves_compiled_http_routes(tmp_path, monkeypatch, demo_seed):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from management.app import app as dashboard
    from research.api import current_lab, install_routes

    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-operator")
    engine = Engine(World(demo_seed))
    lab = current_lab()
    run = lab.create(engine.world, "plain", model=SceneModel())["run_id"]
    plan = lab.plan(run)
    plan["scene"]["services"] = [{
        "id": "catalog", "method": "GET", "path": "catalog/status",
        "node_id": "evidence", "data": {"availability": "ready"},
    }]
    with lab.connect() as db:
        db.execute("UPDATE runs SET plan=? WHERE id=?", (json.dumps(plan), run))
    visitor = FastAPI()
    install_routes(visitor, lambda: engine, surface="visitor")
    public = TestClient(visitor)
    operator = TestClient(dashboard)
    auth = ("operator", "synthetic-operator")
    action = f"/api/evidence/runs/{run}/release/activate"
    assert public.post(action, json={}).status_code == 404
    assert operator.post(action, json={}).status_code == 401
    assert operator.post(action, auth=auth, json={}).status_code == 200
    release = operator.get(f"/api/evidence/runs/{run}", auth=auth).json()["release"]
    path = release["manifest"]["routes"][0]["path"]
    assert public.get(path).json()["data"] == {"availability": "ready"}
    assert public.post(path).status_code == 404

    # The activated manifest is authoritative for the base scene route.
    plan["scene"]["services"][0]["path"] = "catalog/undeployed"
    with lab.connect() as db:
        db.execute("UPDATE runs SET plan=? WHERE id=?", (json.dumps(plan), run))
    assert public.get(f"/decoy/{run}/service/catalog/undeployed").status_code == 404
