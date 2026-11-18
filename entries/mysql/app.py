"""Loopback MySQL protocol projection of a DWS checkpoint."""

import asyncio
import os
import re
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

from mysql_mimic import MysqlServer, Session
from mysql_mimic.auth import Forbidden, IdentityProvider, NativePasswordAuthPlugin, Success, User
from mysql_mimic.errors import MysqlError

from business.atomic_state import read
from business.storage import location
from dws.ids import entity_uuid, ulid_now
from research.lab import MYSQL_CANARY_ID_LENGTH, MYSQL_CANARY_PREFIX

HOST = "db01"
QUERY = "select name, engine from collab_core.catalog"


@dataclass
class CanaryUser(User):
    credential_id: str = ""


class CanaryNativePassword(NativePasswordAuthPlugin):
    """Record successful canary proof without granting a database session."""

    def __init__(self, lab):
        self.lab = lab

    async def auth(self, auth_info=None):
        async with aclosing(super().auth(auth_info)) as exchange:
            result = await anext(exchange)
            if isinstance(result, bytes):
                auth_info = yield result
                result = await exchange.asend(auth_info)
            if isinstance(result, Success) and isinstance(auth_info.user, CanaryUser):
                await asyncio.to_thread(self.lab.observe_canary_proof, auth_info.user.credential_id,
                                        "mysql", ulid_now())
                yield Forbidden()
            else:
                yield result


class CatalogCredentials(IdentityProvider):
    def __init__(self, password, lab=None):
        if not password:
            raise ValueError("HONEYNET_MYSQL_PASSWORD is required")
        self.auth_string = NativePasswordAuthPlugin.create_auth_string(password)
        self.lab = lab

    def get_plugins(self):
        if self.lab is None:
            return super().get_plugins()
        return [CanaryNativePassword(self.lab)]

    async def get_user(self, username):
        if username == "catalog_reader":
            return User(username, self.auth_string, NativePasswordAuthPlugin.name)
        short_id = username.removeprefix(MYSQL_CANARY_PREFIX)
        if self.lab and username.startswith(MYSQL_CANARY_PREFIX) and re.fullmatch(
            rf"[0-9a-f]{{{MYSQL_CANARY_ID_LENGTH}}}", short_id
        ):
            credential = await asyncio.to_thread(self.lab.mysql_canary, short_id)
            if credential:
                credential_id, token = credential
                return CanaryUser(username, NativePasswordAuthPlugin.create_auth_string(token),
                                  NativePasswordAuthPlugin.name, credential_id=credential_id)
        return None


class CatalogSession(Session):
    def __init__(self, checkpoint):
        super().__init__()
        state = read(checkpoint)
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


def create_server(checkpoint, password, lab=None):
    checkpoint = Path(checkpoint)
    CatalogSession(checkpoint)
    async def session_factory():
        return await asyncio.to_thread(CatalogSession, checkpoint)

    return MysqlServer(session_factory=session_factory,
                       identity_provider=CatalogCredentials(password, lab))


async def main():
    checkpoint = location()
    if checkpoint is None or not checkpoint.is_file():
        raise SystemExit("Start the business service and initialize a DWS checkpoint first")
    lab = None
    if os.environ.get("HONEYNET_ENABLE_MYSQL_CANARY") == "1":
        from research.api import current_lab

        lab = current_lab()
    server = create_server(checkpoint, os.environ.get("HONEYNET_MYSQL_PASSWORD", ""), lab)
    await server.start_server(host="127.0.0.1", port=int(os.environ.get("HONEYNET_MYSQL_PORT", "3307")))
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
