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

from audit.alerts import record as record_alert
from business.atomic_state import read
from business.storage import location
from dws.ids import entity_uuid, ulid_now
from research.bindings import MYSQL_PREFIX, resolve
from research.lab import MYSQL_CANARY_ID_LENGTH, MYSQL_CANARY_PREFIX

HOST = "db01"
QUERY = "select name, engine from collab_core.catalog"


@dataclass
class CanaryUser(User):
    credential_id: str = ""


class CatalogNativePassword(NativePasswordAuthPlugin):
    """Observe authentication and reject Canary credentials after proof is recorded."""

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
                result = Forbidden()
            if isinstance(result, Forbidden):
                await asyncio.to_thread(record_alert, "mysql", "login_failed")
            elif isinstance(result, Success):
                await asyncio.to_thread(record_alert, "mysql", "login")
            yield result


class CatalogCredentials(IdentityProvider):
    def __init__(self, password, lab=None, bindings=None):
        if not password:
            raise ValueError("HONEYNET_MYSQL_PASSWORD is required")
        self.auth_string = NativePasswordAuthPlugin.create_auth_string(password)
        self.lab = lab
        self.bindings = bindings

    def get_plugins(self):
        return [CatalogNativePassword(self.lab)]

    async def get_user(self, username):
        if username == "catalog_reader":
            return User(username, self.auth_string, NativePasswordAuthPlugin.name)
        if self.bindings and username.startswith(MYSQL_PREFIX):
            try:
                await asyncio.to_thread(resolve, self.bindings, username.removeprefix(MYSQL_PREFIX))
            except KeyError:
                await asyncio.to_thread(record_alert, "mysql", "login_failed")
                return None
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
        await asyncio.to_thread(record_alert, "mysql", "login_failed")
        return None


class CatalogSession(Session):
    def __init__(self, checkpoint, bindings=None):
        super().__init__()
        self.checkpoint = checkpoint
        self.bindings = bindings
        self.load()

    def load(self, binding=None):
        state = read(self.checkpoint)
        version = state["meta"]["world_version"]
        self.world_version = binding["world_version"] if binding else version
        if binding and (self.world_version > version or binding["world_id"] != state["meta"]["world_id"]):
            raise MysqlError("Catalog version unavailable")
        private = set(state.get("private_versions", ()))

        def visible(rows):
            return next((row for row in reversed(rows or ())
                         if row["world_version"] <= self.world_version and row["world_version"] not in private), None)

        facts = state["facts"]
        host = visible(facts["host"].get(entity_uuid("host", HOST)))
        if not host or not any(service.get("proto") == "mysql" for service in host["payload"]["services"]):
            raise ValueError("MySQL service is absent from the DWS")
        self.rows = []
        for versions in facts["object"].values():
            fact = visible(versions)
            if fact is None:
                continue
            payload = fact["payload"]
            data = payload["data"]
            if payload["type"] == "database" and data.get("host") == HOST:
                self.rows.append((data["name"], data["engine"]))
        self.rows.sort()

    async def init(self, connection):
        await super().init(connection)
        if self.bindings and self.username != "catalog_reader" and self.username.startswith(MYSQL_PREFIX):
            binding = await asyncio.to_thread(resolve, self.bindings, self.username.removeprefix(MYSQL_PREFIX))
            await asyncio.to_thread(self.load, binding)

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


def create_server(checkpoint, password, lab=None, *, bindings=None):
    checkpoint = Path(checkpoint)
    CatalogSession(checkpoint)
    async def session_factory():
        await asyncio.to_thread(record_alert, "mysql", "connection")
        return await asyncio.to_thread(CatalogSession, checkpoint, bindings)

    return MysqlServer(session_factory=session_factory,
                       identity_provider=CatalogCredentials(password, lab, bindings))


async def main():
    checkpoint = location()
    if checkpoint is None or not checkpoint.is_file():
        raise SystemExit("Start the business service and initialize a DWS checkpoint first")
    from research.api import current_lab

    lab = current_lab()
    canaries = lab if os.environ.get("HONEYNET_ENABLE_MYSQL_CANARY") == "1" else None
    server = create_server(checkpoint, os.environ.get("HONEYNET_MYSQL_PASSWORD", ""), canaries, bindings=lab)
    await server.start_server(host="127.0.0.1", port=int(os.environ.get("HONEYNET_MYSQL_PORT", "3307")))
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
