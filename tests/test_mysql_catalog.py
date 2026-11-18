import asyncio

import pymysql
import pytest

from business.enterprise import enrich_seed
from business.storage import persist
from dws.world import World
from entries.mysql.app import QUERY, create_server
from replay.engine import Engine


def test_mysql_client_auth_read_only_and_version_freeze(demo_seed, tmp_path, monkeypatch):
    checkpoint = tmp_path / "world.json"
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
