"""Loopback MySQL protocol projection of a DWS checkpoint."""

import asyncio
import json
import os
import re
from pathlib import Path

from mysql_mimic import MysqlServer, Session
from mysql_mimic.auth import IdentityProvider, NativePasswordAuthPlugin, User
from mysql_mimic.errors import MysqlError

from business.storage import location
from dws.ids import entity_uuid

HOST = "db01"
QUERY = "select name, engine from collab_core.catalog"


class CatalogCredentials(IdentityProvider):
    def __init__(self, password):
        if not password:
            raise ValueError("HONEYNET_MYSQL_PASSWORD is required")
        self.auth_string = NativePasswordAuthPlugin.create_auth_string(password)

    async def get_user(self, username):
        if username == "catalog_reader":
            return User(username, self.auth_string, NativePasswordAuthPlugin.name)
        return None


class CatalogSession(Session):
    def __init__(self, checkpoint):
        super().__init__()
        state = json.loads(checkpoint.read_text())
        self.world_version = state["meta"]["world_version"]
        facts = state["facts"]
        host = facts["host"].get(entity_uuid("host", HOST))
        if not host or not any(service.get("proto") == "mysql" for service in host[-1]["payload"]["services"]):
            raise ValueError("MySQL service is absent from the DWS")
        self.rows = []
        for versions in facts["object"].values():
            payload = versions[-1]["payload"]
            data = payload["data"]
            if payload["type"] == "database" and data.get("host") == HOST:
                self.rows.append((data["name"], data["engine"]))
        self.rows.sort()

    async def handle_query(self, sql, attrs):
        if len(sql) > 512:
            raise MysqlError("Statement is outside the supported catalog subset")
        statement = " ".join(sql.casefold().split()).removesuffix(";")
        if statement == QUERY:
            return self.rows, ["name", "engine"]
        if statement == "select 1":
            return [(1,)], ["1"]
        if re.fullmatch(r"set (names (utf8mb4|utf8mb3|utf8|latin1)( collate [a-z0-9_]+)?|autocommit\s*=\s*[01])", statement):
            return [], []
        raise MysqlError("Statement is outside the supported catalog subset")


def create_server(checkpoint, password):
    checkpoint = Path(checkpoint)
    CatalogSession(checkpoint)
    async def session_factory():
        return await asyncio.to_thread(CatalogSession, checkpoint)

    return MysqlServer(session_factory=session_factory,
                       identity_provider=CatalogCredentials(password))


async def main():
    checkpoint = location()
    if checkpoint is None or not checkpoint.is_file():
        raise SystemExit("Start the business service and initialize a DWS checkpoint first")
    server = create_server(checkpoint, os.environ.get("HONEYNET_MYSQL_PASSWORD", ""))
    await server.start_server(host="127.0.0.1", port=int(os.environ.get("HONEYNET_MYSQL_PORT", "3307")))
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
