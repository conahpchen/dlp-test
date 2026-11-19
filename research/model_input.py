"""Project visitor evidence into a closed vocabulary before model inference."""

import re

KINDS = {
    "research.input", "web.request", "research.visit", "research.delivery",
    "research.beacon", "research.invalid_beacon", "research.lead",
}
ROUTES = {"index", "interact", "beacon", "service/config", "/api/login", "/api/documents"}
COMPONENTS = {"cache", "database"}
INTENTS = {
    "部署回滚": "deployment_rollback",
    "数据库备份": "database_backup",
    "工单": "ticket_lookup",
    "变更记录": "change_lookup",
}


def _enum(value, allow, fallback="unknown"):
    return value if isinstance(value, str) and value in allow else fallback


def model_event(event, plan):
    """Copy only enums, locally generated event IDs, and known scene references."""
    what = event["what"]
    scene = plan.get("scene", {})
    nodes = {node["id"] for node in scene.get("nodes", ())}
    resources = ROUTES | {f'scene/{node}' for node in nodes}
    resources |= {f"service/{service['path']}" for service in scene.get("services", ())}
    resources |= {f"documents/{doc['id']}" for doc in plan["documents"]}
    resource = _enum(what.get("resource"), resources)
    projected = {
        "kind": _enum(what.get("kind"), KINDS),
        "resource": resource,
        "method": _enum(what.get("method"), {"GET", "POST"}),
        "activity": "unknown",
    }
    node = _enum(what.get("node_id"), nodes, None)
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
    event_id = event["event_id"]
    if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", event_id):
        raise ValueError("Invalid internal research event identifier")
    return {"event_id": event_id, "what": projected}
