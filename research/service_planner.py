"""Resolve bounded declarations to local, preconfigured service capabilities."""

import os
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from time import time

from research import isolated_http, isolated_mysql
from research.generation import validate_scene
from research.grounding import materialize_scene


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
    "isolated_instance": Capability(3, "mysql", "local_private_unix_socket", "high"),
    "isolated_http": Capability(3, "http", "local_private_unix_socket", "high"),
}
MAX_HTTP_ROUTES = 8
MAX_NEW_PROCESSES = 1
AUTO_CAPABILITIES = frozenset({"mysql_catalog", "isolated_http", "isolated_mysql"})
PROBE_HEADER = "X-Honeynet-Release-Probe"


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
        with pymysql.connect(host=os.environ.get("HONEYNET_MYSQL_HEALTH_HOST", "127.0.0.1"),
                             port=port, user="catalog_reader", password=password,
                             connect_timeout=2, read_timeout=2, write_timeout=2) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT name, engine FROM collab_core.catalog")
            return bool(cursor.fetchall())
    except (OSError, ValueError, pymysql.MySQLError):
        return False


def isolated_runtime(plan):
    return isolated_http if plan.get("scene", {}).get("isolated_reason") == "standalone_http" else isolated_mysql


def automatic_capabilities():
    requested = set(os.environ.get("RESEARCH_AUTO_SERVICE_CAPABILITIES", "").split(",")) - {""}
    if not requested <= AUTO_CAPABILITIES:
        raise ValueError("Unsupported automatic service capability")
    return requested


def requests_for(scene):
    selected = set(scene.get("capability_requests", []))
    if "isolated_instance" in selected:
        selected.remove("isolated_instance")
        selected.add("isolated_http" if scene["isolated_reason"] == "standalone_http" else "isolated_mysql")
    return selected


def prepare_release(plan, probe=None, *, approve_isolated=False):
    try:
        scene = validate_scene(plan["scene"], plan["documents"], plan.get("entity_graph"),
                               typed_facts=bool(plan.get("fact_schema")))
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
    requests = scene.capability_requests
    if "mysql_catalog" in requests:
        if not (probe or mysql_catalog_ready)():
            raise ReleasePreparationError("mysql_unready", "The configured MySQL catalog failed protocol readiness")
        selected.append("mysql_catalog")
    if "isolated_instance" in requests:
        if not approve_isolated:
            raise ReleasePreparationError("approval_required", "No approved isolated runtime for this release")
        if not scene.isolated_reason:
            raise ReleasePreparationError("missing_requirement", "An isolated runtime requires an explicit capability requirement")
        try:
            isolated_runtime(plan).start(dict(plan, scene=materialize_scene(scene.model_dump())))
        except isolated_mysql.RuntimeUnavailable as exc:
            raise ReleasePreparationError("runtime_unavailable", str(exc)) from None
        selected.append("isolated_http" if isolated_runtime(plan) is isolated_http else "isolated_instance")
    return {
        "world_version": plan["world_version"],
        "capabilities": [{"name": name, **asdict(CAPABILITIES[name])} for name in selected],
        "routes": services,
        "new_processes": MAX_NEW_PROCESSES if "isolated_instance" in requests else 0,
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
            host = os.environ.get("HONEYNET_VISITOR_HEALTH_HOST", "127.0.0.1")
            reply = client.get(f"http://{host}:{port}/healthz")
            return reply.status_code == 200 and reply.json() == {"service": "honeynet-visitor"}
    except (httpx.HTTPError, OSError, ValueError):
        return False


def scene_resource(scene, resource, method, manifest):
    service = next((item for item in scene.get("services", ())
                    if resource == "service/" + item["path"] and method == item["method"]), None)
    if service and manifest is not None:
        if not any(route["id"] == service["id"] and route["method"] == method
                   and route["path"] == resource and route["node_id"] == service["node_id"]
                   for route in manifest):
            service = None
    if service:
        node_id = service["node_id"]
    elif resource == "index":
        node_id = scene["entry_node"]
    elif resource.startswith("scene/"):
        node_id = resource.removeprefix("scene/")
    else:
        node_id = None
    return next((node for node in scene["nodes"] if node["id"] == node_id), None), service


def preview(plan, manifest, resource, method):
    from research.grounding import materialize_scene

    scene = materialize_scene(plan["scene"])
    root = f"/decoy/{plan['run_id']}/"
    routes = [dict(route, path=route["path"].removeprefix(root)) for route in manifest["routes"]]
    node, service = scene_resource(scene, resource, method, routes)
    if node is None or resource.startswith("service/") and service is None:
        return None
    return dict(world_version=manifest.get("graph_world_version", manifest["world_version"]),
                node=node["id"], service=service["id"] if service else None,
                data=service["data"] if service else None)


def verify_candidate(lab, plan, manifest):
    """Compare actual dispatch against the compiled manifest before traffic switches."""
    import httpx

    from research.grounding import materialize_scene

    scene = materialize_scene(plan["scene"])
    version = manifest.get("graph_world_version", manifest["world_version"])
    root = f"/decoy/{plan['run_id']}/"
    cases = [("GET", "index", dict(world_version=version, node=scene["entry_node"], service=None, data=None))]
    cases.extend((item["method"], "service/" + item["path"],
                  dict(world_version=version, node=item["node_id"], service=item["id"], data=item["data"]))
                 for item in scene["services"])
    port = os.environ.get("HONEYNET_VISITOR_HEALTH_PORT")
    # Legacy scenes do not have a separately versioned published graph. Verify
    # their manifest locally, while new split deployments exercise the listener.
    if not port or "graph_world_version" not in manifest:
        if any(preview(plan, manifest, resource, method) != expected for method, resource, expected in cases):
            raise ReleasePreparationError("route_unready", "Candidate route failed verification")
        return

    token = secrets.token_hex(32)
    key = hashlib.sha256(token.encode()).hexdigest()
    with lab.connect() as db:
        db.execute("DELETE FROM release_probes WHERE expires<=?", (int(time()),))
        db.execute("INSERT INTO release_probes VALUES (?,?,?,?)",
                   (plan["run_id"], key, int(time()) + 30, json.dumps(manifest)))
    try:
        host = os.environ.get("HONEYNET_VISITOR_HEALTH_HOST", "127.0.0.1")
        with httpx.Client(base_url=f"http://{host}:{int(port)}", timeout=2, trust_env=False) as client:
            for method, resource, expected in cases:
                response = client.request(method, root + resource, headers={PROBE_HEADER: token})
                if response.status_code != 200 or response.json() != expected:
                    raise ReleasePreparationError("route_unready", "Candidate route failed verification")
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, ReleasePreparationError):
            raise
        raise ReleasePreparationError("route_unready", "Candidate route failed verification") from None
    finally:
        with lab.connect() as db:
            db.execute("DELETE FROM release_probes WHERE run=? AND token_hash=?", (plan["run_id"], key))


def unavailable(plan, manifest):
    checks = {
        "knowledge_documents": lambda scene: bool(scene.get("documents")),
        "http_routes": http_listener_ready,
        "mysql_catalog": lambda _scene: mysql_catalog_ready(),
        "isolated_instance": lambda scene: isolated_mysql.ready(scene["run_id"]),
        "isolated_http": lambda scene: isolated_http.ready(scene["run_id"]),
    }
    return sorted({item["name"] for item in manifest["capabilities"]
                   if item["name"] not in checks or not checks[item["name"]](plan)})
