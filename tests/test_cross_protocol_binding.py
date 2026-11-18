import asyncio
import copy

import pymysql
from fastapi.testclient import TestClient

from business import app as visitor
from business.storage import persist
from entries.mysql.app import QUERY, create_server
from management.app import app as dashboard
from research.api import current_lab
from research.bindings import HEADER
from tests.test_generative_research import SceneModel


def test_approved_binding_keeps_web_fake_and_mysql_on_same_version(monkeypatch, tmp_path):
    checkpoint = tmp_path / "world.sqlite"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(checkpoint))
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-operator")
    monkeypatch.setattr(visitor, "_engine", None)
    monkeypatch.setattr(visitor, "_audit_rows", [])
    monkeypatch.setattr(visitor, "_events", [])
    monkeypatch.setattr(visitor, "_event_ids", set())
    engine = visitor.get_engine()
    lab = current_lab()
    run = lab.create(engine.world, "plain", model=SceneModel())["run_id"]
    lab.activate(run)
    admin = TestClient(dashboard)
    endpoint = f"/api/evidence/runs/{run}/bindings"
    assert admin.post(endpoint, json={}).status_code == 401
    result = admin.post(endpoint, auth=("operator", "synthetic-operator"), json={})
    assert result.status_code == 201
    binding = result.json()
    token = binding["binding"]

    ai = next(f for f in engine.world.all_current_cls("host") if f.payload["hostname"] == "ai01")
    host = copy.deepcopy(ai.payload)
    next(s for s in host["services"] if s["proto"] == "openai")["ver"] = "corp-new"
    old = next(f for f in engine.world.all_current_cls("object") if f.payload["type"] == "database")
    database = dict(old.payload, data=dict(old.payload["data"], engine="MySQL 8.0.99"))
    old_database_engine = old.payload["data"]["engine"]
    engine.commit_external(None, [("host", host), ("object", database)])
    persist(engine, [], [])
    newer = lab.create(engine.world, "plain", model=SceneModel())["run_id"]
    lab.activate(newer)

    public = TestClient(visitor.app)
    bound_headers = {HEADER: token}
    assert public.get("/", headers=bound_headers, follow_redirects=False).headers["location"].endswith(run + "/index")
    assert public.get("/", follow_redirects=False).headers["location"].endswith(run + "/index")
    assert public.get("/v1/models", headers=bound_headers).status_code == 401
    api_headers = dict(bound_headers, Authorization="Bearer sk-demo-internal-2026")
    assert public.get("/v1/models", headers=api_headers).json()["data"][0]["id"] == "corp-llm-7b"
    assert public.get("/v1/models").json()["data"][0]["id"] == "corp-new"

    async def check_mysql():
        server = create_server(checkpoint, "synthetic-catalog", bindings=lab)
        await server.start_server(host="127.0.0.1", port=0)
        port = server.sockets()[0].getsockname()[1]

        def catalog(user):
            with pymysql.connect(host="127.0.0.1", port=port, user=user,
                                 password="synthetic-catalog", connect_timeout=3,
                                 read_timeout=3, write_timeout=3) as connection, connection.cursor() as cursor:
                cursor.execute(QUERY)
                return cursor.fetchone()[1]

        try:
            assert await asyncio.to_thread(catalog, binding["mysql_user"]) == old_database_engine
            assert await asyncio.to_thread(catalog, "catalog_reader") == "MySQL 8.0.99"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(check_mysql())
