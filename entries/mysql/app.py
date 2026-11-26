"""Loopback MySQL protocol projection of a DWS checkpoint."""

import asyncio
import os
import re
import sqlite3
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from mysql_mimic import MysqlServer, Session
from mysql_mimic.auth import Forbidden, IdentityProvider, NativePasswordAuthPlugin, Success, User
from mysql_mimic.errors import MysqlError

from agent_runtime.service_identity import service_secret
from audit.alerts import record as record_alert
from audit.flow_evidence import observe_direct
from business.atomic_state import read
from business.storage import location
from dws.ids import entity_uuid, ulid_now
from entries.ingress_limits import BurstBudget, configured_limit, socket_peer
from research import handoffs
from research.bindings import MYSQL_PREFIX, resolve
from research.lab import MYSQL_CANARY_ID_LENGTH, MYSQL_CANARY_PREFIX

HOST = "db01"
QUERY = "select name, engine from collab_core.catalog"
HANDOFF_QUERY = "select next_hop from collab_core.handoff"
transport_session = ContextVar("mysql_transport_session")
restricted_grant = ContextVar("mysql_restricted_grant", default=None)
delivery = ContextVar("mysql_delivery", default=None)


class ReceiptWriter:
    def __init__(self, writer):
        self.writer = writer
        self.receipt = None

    def __getattr__(self, name):
        return getattr(self.writer, name)

    def write(self, data):
        self.writer.write(data)
        pending = delivery.get()
        if pending and pending[2] in data:
            self.receipt = pending[:2]
            delivery.set(None)

    async def drain(self):
        await self.writer.drain()
        if self.receipt:
            await asyncio.to_thread(handoffs.delivered, *self.receipt)
            self.receipt = None


class CatalogServer(MysqlServer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.connection_budget = BurstBudget(configured_limit("HONEYNET_MYSQL_CONNECTIONS_PER_10S"))

    async def _client_connected_cb(self, reader, writer):
        peer = writer.get_extra_info("peername")
        rejected, first = self.connection_budget.check(socket_peer(peer))
        if rejected:
            writer.close()
            if first:
                await asyncio.to_thread(record_alert, "mysql", "rate_limited")
            return
        session = ulid_now()
        context = transport_session.set(session)
        try:
            await asyncio.to_thread(observe_direct, "mysql", session,
                                    peer, writer.get_extra_info("sockname"))
            await super()._client_connected_cb(reader, ReceiptWriter(writer) if handoffs.enabled() else writer)
        finally:
            transport_session.reset(context)
            writer.close()


@dataclass
class CanaryUser(User):
    credential_id: str = ""
    directed: bool = False


class EmployeeUser(User):
    pass


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
                if auth_info.user.directed and handoffs.enabled():
                    accepted, child = await asyncio.to_thread(
                        handoffs.accept, self.lab, auth_info.user.credential_id, "mysql", transport_session.get()
                    )
                    if accepted and child:
                        restricted_grant.set((self.lab, child))
                    else:
                        result = Forbidden()
                else:
                    await asyncio.to_thread(self.lab.observe_canary_proof, auth_info.user.credential_id,
                                            "mysql", transport_session.get())
                    result = Forbidden()
            if isinstance(result, Forbidden):
                await asyncio.to_thread(record_alert, "mysql", "login_failed")
            elif isinstance(result, Success) and not isinstance(auth_info.user, EmployeeUser):
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
        employee_password = service_secret("mysql", username)
        if employee_password:
            return EmployeeUser(username, NativePasswordAuthPlugin.create_auth_string(employee_password),
                                NativePasswordAuthPlugin.name)
        if self.bindings and username.startswith(MYSQL_PREFIX):
            try:
                await asyncio.to_thread(resolve, self.bindings, username.removeprefix(MYSQL_PREFIX), entry="mysql")
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
                credential_id, token, directed = credential
                return CanaryUser(username, NativePasswordAuthPlugin.create_auth_string(token),
                                  NativePasswordAuthPlugin.name, credential_id=credential_id, directed=directed)
        await asyncio.to_thread(record_alert, "mysql", "login_failed")
        return None


class CatalogSession(Session):
    def __init__(self, checkpoint, bindings=None):
        super().__init__()
        self.checkpoint = checkpoint
        self.bindings = bindings
        self.query_budget = BurstBudget(configured_limit("HONEYNET_MYSQL_QUERIES_PER_10S"))
        self.load()

    def load(self, binding=None):
        state = read(self.checkpoint)
        version = state["meta"]["world_version"]
        self.world_version = binding["world_version"] if binding else version
        if binding and (self.world_version > version or binding["world_id"] != state["meta"]["world_id"]):
            raise MysqlError("Catalog version unavailable")
        private = set(state.get("private_versions", ()))
        overlay = binding.get("branch_version") if binding else None
        if overlay is not None and overlay not in private:
            raise MysqlError("Catalog branch unavailable")

        def visible(rows):
            return next((row for row in reversed(rows or ())
                         if row["world_version"] == overlay or
                         (row["world_version"] <= self.world_version and row["world_version"] not in private)), None)

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
            if payload["type"] == "database" and data.get("host") == HOST and not data.get("graph_retired"):
                self.rows.append((data["name"], data["engine"]))
        self.rows.sort()

    async def init(self, connection):
        await super().init(connection)
        if not self.username:
            connection.stream.writer.close()
            return
        if self.bindings and self.username.startswith(MYSQL_PREFIX) and self.username != "catalog_reader":
            binding = await asyncio.to_thread(resolve, self.bindings, self.username.removeprefix(MYSQL_PREFIX),
                                              entry="mysql")
            await asyncio.to_thread(self.load, binding)

    async def handle_query(self, sql, attrs):
        if not self.username:
            raise MysqlError("Authentication required")
        rejected, _ = self.query_budget.check("query")
        if rejected:
            self.connection.stream.writer.close()
            await asyncio.to_thread(record_alert, "mysql", "session_ended")
            return [], []
        if len(sql) > 512:
            raise MysqlError("Statement is outside the supported catalog subset")
        statement = " ".join(sql.casefold().split()).removesuffix(";")
        grant = restricted_grant.get()
        if grant and statement == HANDOFF_QUERY:
            lab, (identifier, target) = grant
            if not await asyncio.to_thread(handoffs.permits_for, lab, identifier):
                raise MysqlError("Verification is unavailable")
            key = target["verification_key"]
            delivery.set((lab, identifier, key.encode()))
            return [(key,)], ["next_hop"]
        if re.fullmatch(r"set (names (utf8mb4|utf8mb3|utf8|latin1)( collate [a-z0-9_]+)?|autocommit\s*=\s*[01])", statement):
            return [], []
        if grant or self.username.startswith(MYSQL_CANARY_PREFIX):
            raise MysqlError("Statement is outside the supported verification subset")
        if statement == QUERY:
            from research.worker import automatic_enabled

            if self.bindings and automatic_enabled() and not service_secret("mysql", self.username):
                from research.protocol_decoys import available, observe

                if not hasattr(self, "decoy_session"):
                    self.decoy_session = transport_session.get()
                try:
                    decoy = await asyncio.to_thread(available, self.bindings, "mysql", self.decoy_session)
                    await asyncio.to_thread(observe, self.bindings, "mysql", self.decoy_session, "list_catalog")
                    if decoy:
                        name = "reference_" + decoy["title"].replace("-", "_")
                        if all(row[0] != name for row in self.rows):
                            return [*self.rows, (name, "MySQL")], ["name", "engine"]
                except sqlite3.Error:
                    pass
            return self.rows, ["name", "engine"]
        if statement == "select 1":
            return [(1,)], ["1"]
        raise MysqlError("Statement is outside the supported catalog subset")


def create_server(checkpoint, password, lab=None, *, bindings=None):
    checkpoint = Path(checkpoint)
    CatalogSession(checkpoint)
    async def session_factory():
        await asyncio.to_thread(record_alert, "mysql", "connection")
        return await asyncio.to_thread(CatalogSession, checkpoint, bindings)

    return CatalogServer(session_factory=session_factory,
                       identity_provider=CatalogCredentials(password, lab, bindings))


async def main():
    checkpoint = location()
    if checkpoint is None or not checkpoint.is_file():
        raise SystemExit("Start the business service and initialize a DWS checkpoint first")
    from research.api import current_lab

    lab = current_lab()
    canaries = lab if handoffs.enabled() or os.environ.get("HONEYNET_ENABLE_MYSQL_CANARY") == "1" else None
    server = create_server(checkpoint, os.environ.get("HONEYNET_MYSQL_PASSWORD", ""), canaries, bindings=lab)
    await server.start_server(host=os.environ.get("HONEYNET_MYSQL_BIND", "127.0.0.1"),
                              port=int(os.environ.get("HONEYNET_MYSQL_PORT", "3307")))
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
