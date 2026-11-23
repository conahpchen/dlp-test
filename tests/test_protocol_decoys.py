import asyncio
import json

import pymysql
from fastapi.testclient import TestClient

from business import app as business
from business.enterprise import enrich_seed
from business.storage import persist
from dws.world import World
from entries.mysql.app import QUERY, create_server
from replay.engine import Engine
from research.api import current_lab
from research.protocol_decoys import process_one


class ProtocolModel:
    def choose(self, messages):
        observation = json.loads(messages[-1]["content"])
        entry = observation["protocol"]
        if entry == "llm":
            assert observation == {"protocol": "llm", "activities": ["doc"]}
            assert "doc means a request for internal documentation" in messages[0]["content"]
            assert "介绍运维文档" not in json.dumps(messages, ensure_ascii=False)
        return {"intent": "catalog", "title": entry + "_inventory",
                "note": "Inventory reference for internal operations."}


def test_fake_api_adapts_authenticated_session_only(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("BUSINESS_STATE_FILE", "")
    monkeypatch.setenv("RESEARCH_AUTOMATIC_DECOYS", "1")
    monkeypatch.setattr(business, "_engine", None)
    client = TestClient(business.app)
    body = {"model": "corp-llm-7b", "messages": [{"role": "user", "content": "介绍运维文档"}]}

    def request(label, actor=client):
        response = actor.post("/v1/chat/completions", json=body, headers={
            "Authorization": "Bearer sk-demo-internal-2026", "X-Session": label,
        })
        assert response.status_code == 200
        return response.json()["choices"][0]["message"]["content"]

    assert "Inventory reference" not in request("first")
    assert process_one(current_lab(), ProtocolModel())
    assert "Inventory reference" in request("first")
    assert "Inventory reference" not in request("second")
    with TestClient(business.app) as another_client:
        assert "Inventory reference" not in request("first", another_client)


def test_mysql_adapts_catalog_for_one_connection(demo_seed, tmp_path, monkeypatch):
    checkpoint = tmp_path / "world.json"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(checkpoint))
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("RESEARCH_AUTOMATIC_DECOYS", "1")
    persist(Engine(World(enrich_seed(demo_seed))), [], [])
    lab = current_lab()

    async def check():
        server = create_server(checkpoint, "synthetic", bindings=lab)
        await server.start_server(host="127.0.0.1", port=0)
        port = server.sockets()[0].getsockname()[1]

        def connect():
            return pymysql.connect(host="127.0.0.1", port=port, user="catalog_reader",
                                   password="synthetic", connect_timeout=2, read_timeout=2)

        def exercise():
            with connect() as db, db.cursor() as cursor:
                cursor.execute(QUERY)
                assert all(row[0] != "reference_mysql_inventory" for row in cursor.fetchall())
                assert process_one(lab, ProtocolModel())
                cursor.execute(QUERY)
                assert ("reference_mysql_inventory", "MySQL") in cursor.fetchall()
            with connect() as db, db.cursor() as cursor:
                cursor.execute(QUERY)
                assert all(row[0] != "reference_mysql_inventory" for row in cursor.fetchall())

        try:
            await asyncio.to_thread(exercise)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(check())
