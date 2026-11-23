"""Project visitor evidence into a closed vocabulary before model inference."""

import re

KINDS = {
    "research.input", "web.request", "research.visit", "research.delivery",
    "research.beacon", "research.invalid_beacon", "research.lead",
}
ROUTES = {"index", "interact", "beacon", "service/config", "/api/login", "/api/documents", "/api/databases", "/api/assets", "/api/tickets"}
COMPONENTS = {"cache", "database"}
INTENTS = {
    "部署回滚": "deployment_rollback",
    "数据库备份": "database_backup",
    "工单": "ticket_lookup",
    "变更记录": "change_lookup",
}


def _enum(value, allow, fallback="unknown"):
    return value if isinstance(value, str) and value in allow else fallback


def scene_reference(what, scene):
    """Resolve server-declared members against the exact scene used for the event."""
    nodes = {node["id"] for node in scene.get("nodes", ())}
    resources = {f"scene/{node}" for node in nodes}
    services = {f"service/{service['path']}": service["node_id"] for service in scene.get("services", ())
                if not what.get("method") or what["method"] == service.get("method")}
    resources |= services.keys()
    resource = _enum(what.get("resource"), resources, None)
    node = _enum(what.get("node_id"), nodes, services.get(resource))
    return {key: value for key, value in (("resource", resource), ("node_id", node)) if value is not None}


def model_event(event, plan):
    """Copy only enums, locally generated event IDs, and known scene references."""
    what = event["what"]
    scene = plan.get("scene") or {}
    nodes = {node["id"] for node in scene.get("nodes", ())}
    resources = ROUTES | {f'scene/{node}' for node in nodes}
    resources |= {f"service/{service['path']}" for service in scene.get("services", ())}
    resources |= {f"documents/{doc['id']}" for doc in plan["documents"]}
    observed = what.get("observed_scene")
    # New events carry the locally resolved scene member. Legacy events keep
    # their original conservative base-scene projection.
    if isinstance(observed, dict):
        resource = observed.get("resource") or _enum(what.get("resource"), ROUTES | {
            f"documents/{doc['id']}" for doc in plan["documents"]
        })
        node = observed.get("node_id")
    else:
        resource = _enum(what.get("resource"), resources)
        node = _enum(what.get("node_id"), nodes, None)
    projected = {
        "kind": _enum(what.get("kind"), KINDS),
        "resource": resource,
        "method": _enum(what.get("method"), {"GET", "POST"}),
        "activity": "unknown",
    }
    if node is not None:
        projected["node_id"] = node
    inputs = what.get("input") or {}
    for field in ("query", "body"):
        value = inputs.get(field)
        if isinstance(value, dict):
            component = _enum(value.get("component"), COMPONENTS, None)
            if component is not None:
                projected["activity"] = "component_lookup"
                projected["component"] = component
            value = value.get("message")
        if isinstance(value, str):
            for phrase, intent in INTENTS.items():
                if phrase in value:
                    projected["activity"] = intent
                    break
    if inputs.get("truncated") is True:
        projected["activity"] = "truncated"
    elif (projected["activity"] == "unknown" and projected["kind"] == "research.input"
          and isinstance(observed, dict) and observed.get("resource", "").startswith("service/")):
        projected["activity"] = "service_access"
    event_id = event["event_id"]
    if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", event_id):
        raise ValueError("Invalid internal research event identifier")
    return {"event_id": event_id, "what": projected}
