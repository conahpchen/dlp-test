"""Resolve bounded declarations to local, preconfigured service capabilities."""

import os
from dataclasses import asdict, dataclass

from research.generation import validate_scene


@dataclass(frozen=True)
class Capability:
    level: int
    protocol: str
    exposure: str
    risk: str


CAPABILITIES = {
    "knowledge_documents": Capability(0, "embedded", "existing_scene", "low"),
    "http_routes": Capability(1, "http", "existing_visitor_listener", "low"),
    "mysql_catalog": Capability(2, "mysql", "configured_loopback", "moderate"),
    "isolated_instance": Capability(3, "unsupported", "unavailable", "high"),
}
MAX_HTTP_ROUTES = 8
MAX_NEW_PROCESSES = 0


class ReleasePreparationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def mysql_catalog_ready():
    import pymysql

    password = os.environ.get("HONEYNET_MYSQL_HEALTH_PASSWORD")
    if not password:
        return False
    try:
        port = int(os.environ.get("HONEYNET_MYSQL_PORT", "3307"))
        if not 1 <= port <= 65535:
            return False
        with pymysql.connect(host="127.0.0.1", port=port, user="catalog_reader", password=password,
                             connect_timeout=2, read_timeout=2, write_timeout=2) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT name, engine FROM collab_core.catalog")
            return bool(cursor.fetchall())
    except (OSError, ValueError, pymysql.MySQLError):
        return False


def prepare_release(plan, probe=None):
    try:
        scene = validate_scene(plan["scene"], plan["documents"])
    except (KeyError, ValueError):
        raise ReleasePreparationError("invalid_scene", "Scene failed release validation") from None
    if len(scene.services) > MAX_HTTP_ROUTES:
        raise ReleasePreparationError("route_budget", "HTTP route budget exceeded")
    services = [{
        "id": service.id, "method": service.method,
        "path": "/decoy/" + plan["run_id"] + "/service/" + service.path,
        "node_id": service.node_id,
    } for service in scene.services]
    if not http_listener_ready(plan):
        raise ReleasePreparationError("http_unready", "The configured HTTP listener failed readiness")
    selected = ["knowledge_documents", "http_routes"]
    for request in scene.capability_requests:
        if request == "isolated_instance":
            raise ReleasePreparationError("runtime_unavailable", "No approved isolated runtime is available")
        if not (probe or mysql_catalog_ready)():
            raise ReleasePreparationError("mysql_unready", "The configured MySQL catalog failed protocol readiness")
        selected.append(request)
    return {
        "world_version": plan["world_version"],
        "capabilities": [{"name": name, **asdict(CAPABILITIES[name])} for name in selected],
        "routes": services,
        "new_processes": MAX_NEW_PROCESSES,
    }


def http_listener_ready(_plan):
    import httpx

    configured = os.environ.get("HONEYNET_VISITOR_HEALTH_PORT")
    if not configured:
        return True  # Legacy integrated deployment has no separate listener.
    try:
        port = int(configured)
        if not 1 <= port <= 65535:
            return False
        with httpx.Client(timeout=2, trust_env=False) as client:
            reply = client.get(f"http://127.0.0.1:{port}/healthz")
            return reply.status_code == 200 and reply.json() == {"service": "honeynet-visitor"}
    except (httpx.HTTPError, OSError, ValueError):
        return False


def unavailable(plan, manifest):
    checks = {
        "knowledge_documents": lambda scene: bool(scene.get("documents")),
        "http_routes": http_listener_ready,
        "mysql_catalog": lambda _scene: mysql_catalog_ready(),
    }
    return sorted({item["name"] for item in manifest["capabilities"]
                   if item["name"] not in checks or not checks[item["name"]](plan)})
