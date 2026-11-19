import asyncio

import pymysql
import pytest

from business.enterprise import enrich_seed
from business.storage import persist
from dws.world import World
from entries.mysql.app import QUERY, create_server
from replay.engine import Engine


def test_mysql_client_auth_read_only_and_version_freeze(demo_seed, tmp_path, monkeypatch):
    monkeypatch.setenv("HONEYNET_ALERTS_FILE", str(tmp_path / "alerts.sqlite"))
    checkpoint = tmp_path / "world.sqlite"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(checkpoint))
    engine = Engine(World(enrich_seed(demo_seed)))
    persist(engine, [], [])

    async def run():
        server = create_server(checkpoint, "synthetic-password")
        await server.start_server(host="127.0.0.1", port=0)
        port = server.sockets()[0].getsockname()[1]

        def connect(password):
            return pymysql.connect(host="127.0.0.1", port=port, user="catalog_reader",
                                   password=password, autocommit=True, connect_timeout=3,
                                   read_timeout=3, write_timeout=3)

        def check():
            with pytest.raises(pymysql.MySQLError):
                connect("different-password")
            with connect("synthetic-password") as old, old.cursor() as cursor:
                cursor.execute(QUERY)
                assert cursor.fetchall() == (("collab_core", "MySQL 8.0.35"),)
                database = next(f for f in engine.world.all_current_cls("object")
                                if f.payload["type"] == "database")
                engine.commit_external(None, [("object", dict(
                    database.payload, data=dict(database.payload["data"], engine="MySQL 8.0.36")
                ))])
                persist(engine, [], [])
                cursor.execute(QUERY)
                assert cursor.fetchall()[0][1] == "MySQL 8.0.35"
                with connect("synthetic-password") as fresh, fresh.cursor() as newer:
                    newer.execute(QUERY)
                    assert newer.fetchall()[0][1] == "MySQL 8.0.36"
                    with pytest.raises(pymysql.MySQLError):
                        newer.execute("DROP TABLE collab_core.catalog")

        try:
            await asyncio.to_thread(check)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
    from audit.alerts import read_alerts

    kinds = [item["kind"] for item in read_alerts("mysql")["alerts"]]
    assert kinds.count("connection") == 3
    assert kinds.count("login_failed") == 1
    assert kinds.count("login") == 2


def test_mysql_verified_canary_proof_is_attributed_without_catalog_access(demo_seed, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from research.api import current_lab, install_routes

    checkpoint = tmp_path / "world.json"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(checkpoint))
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_ENABLE_MYSQL_CANARY", "1")
    monkeypatch.setenv("HONEYNET_CONTROL_TOKEN", "synthetic-control")
    engine = Engine(World(enrich_seed(demo_seed)))
    persist(engine, [], [])
    lab = current_lab()
    public = FastAPI()
    install_routes(public, lambda: engine, surface="visitor")
    run = lab.create(engine.world, "agent_note")["run_id"]
    visitor = TestClient(public)
    response = visitor.get(f"/decoy/{run}/documents/0")
    assert response.status_code == 200
    proof = response.json()["mysql_verification"]
    credential = lab.canaries(run)[0]
    control = FastAPI()
    install_routes(control, lambda: engine, surface="control")
    operator = TestClient(control)
    revoke_path = f"/api/research/canaries/{credential['credential_id']}/revoke"

    async def exchange():
        server = create_server(checkpoint, "catalog-password", lab)
        await server.start_server(host="127.0.0.1", port=0)
        port = server.sockets()[0].getsockname()[1]

        def probe(password):
            with pytest.raises(pymysql.MySQLError):
                pymysql.connect(host="127.0.0.1", port=port, user=proof["username"],
                                password=password, connect_timeout=3, read_timeout=3, write_timeout=3)

        def check_catalog_access():
            with pymysql.connect(host="127.0.0.1", port=port, user="catalog_reader",
                                 password="catalog-password", connect_timeout=3,
                                 read_timeout=3, write_timeout=3) as conn, conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)

        try:
            await asyncio.to_thread(check_catalog_access)
            await asyncio.to_thread(probe, "incorrect")
            with lab.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM lure_uses").fetchone()[0] == 0
            await asyncio.to_thread(probe, proof["password"])
            assert operator.post(revoke_path).status_code == 404
            assert operator.post(revoke_path, headers={"x-control-token": "synthetic-control"}).status_code == 200
            await asyncio.to_thread(probe, proof["password"])
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(exchange())
    with lab.connect() as db:
        assert db.execute("SELECT token_hash,entry FROM lure_uses").fetchall() == [
            (credential["credential_id"], "mysql"),
        ]
    assert visitor.get(response.json()["verification_link"]).status_code == 404
