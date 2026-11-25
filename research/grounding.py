"""Small read-only entity graph supplied to the model, with typed anchor validation."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Anchor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: str
    entity_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    relation: Literal["documents", "reviews", "uses"]


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    entity_id: str
    field: Literal["label", "hosted_on", "supports", "engine", "service_version", "world_version", "access_scope"]
    protocol: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,20}$")
    value: str = Field(min_length=1, max_length=160)


SLOT = re.compile(r"\[\[dws:([a-z][a-z0-9-]{0,31})\]\]")
INFRA_NAME = re.compile(
    r"(?i)(?<![\w-])(?:(?:[a-z0-9]+-)*(?:host|node|web|db|srv|ssh|ai|oa|app)[-]?[0-9]+|app-[a-z][a-z0-9-]+)(?![\w-])"
)
CRITICAL_PROSE = re.compile(
    r"(?i)\b(?:host(?:name)?|server|port|scope|version|network|engine|access scope)\s*[:=]"
    r"|\b(?:hosted on|running on|deployed on)\s+\S+"
    r"|(?:主机|宿主|端口|监听地址|权限范围|引擎版本|世界版本)\s*[:：=]"
    r"|(?:部署于|部署在|运行于|运行在|托管于|托管在|监听端口)\s*[\w-]+"
)
QUALIFIED_SLOT = re.compile(
    r"(?i:(?P<qualifier>\b(?:host(?:name)?|server|port|scope|version|network|engine|access scope)"
    r"|\b(?:hosted on|running on|deployed on)\b|(?:主机|宿主|端口|监听地址|权限范围|引擎版本|世界版本"
    r"|部署于|部署在|运行于|运行在|托管于|托管在|监听端口)))\s*[:：=]?"
    r"\s*" + SLOT.pattern
)
CRITICAL_FIELDS = {
    "host": {"hosted_on", "label"}, "hostname": {"hosted_on", "label"}, "hosted_on": {"hosted_on"},
    "server": {"hosted_on", "label"}, "db_host": {"hosted_on", "label"},
    "application": {"supports", "label"}, "database": {"label"}, "engine": {"engine"},
    "version": {"service_version", "engine", "world_version"}, "service_version": {"service_version"},
    "world_version": {"world_version"}, "access_scope": {"access_scope"}, "scope": {"access_scope"},
    "port": set(), "ip": set(), "permissions": set(), "host_ip": set(), "endpoint": set(),
}


def project(world, version, documents=()):
    view = world.view(version)
    document_ids = {doc["fact_id"] for doc in documents}
    hosts = sorted(view.all_current_cls("host"), key=lambda f: f.payload["hostname"])[:8]
    objects = [fact for fact in view.all_current_cls("object")
               if fact.payload["type"] in ("application", "database") and not fact.payload["data"].get("graph_retired")]
    objects = sorted(objects, key=lambda f: (f.payload["type"], f.payload["object_key"]))[:10]
    selected = [dict(id=f.entity_id, kind="host", label=f.payload["hostname"],
                     protocols=sorted({s["proto"] for s in f.payload["services"]})) for f in hosts]
    selected.extend(dict(id=f.entity_id, kind=f.payload["type"], label=f.payload["object_key"]) for f in objects)
    selected.extend(dict(id=f.entity_id, kind="knowledge", label=f.payload["path"])
                    for f in view.all_current_cls("data") if f.fact_id in document_ids)
    ids = {entity["id"] for entity in selected}
    for fact, entity in zip(hosts, selected, strict=False):
        versions = {}
        for service in fact.payload["services"]:
            versions.setdefault(service["proto"], set()).add(service["ver"])
        entity["service_versions"] = {proto: next(iter(values)) for proto, values in versions.items()
                                      if len(values) == 1 and next(iter(values))}
    object_facts = {row.entity_id: row.payload["data"] for row in objects}
    for entity in selected:
        entity["world_version"] = str(version)
        entity["access_scope"] = "synthetic_only"
        if engine := object_facts.get(entity["id"], {}).get("engine"):
            entity["engine"] = engine
    edges = [dict(source=f.payload["from_entity_id"], target=f.payload["to_entity_id"],
                  relation=f.payload["kind"]) for f in view.all_current_cls("relation")
             if f.payload["from_entity_id"] in ids and f.payload["to_entity_id"] in ids]
    labels = {entity["label"]: entity["id"] for entity in selected}
    for fact in objects:
        for key, relation in (("host", "hosted_on"), ("application", "supports")):
            if target := labels.get(fact.payload["data"].get(key)):
                edges.append(dict(source=fact.entity_id, target=target, relation=relation))
    return {"entities": selected, "relations": edges[:30]}


RELATION_TARGETS = {
    "documents": {"host", "database", "application", "knowledge"},
    "reviews": {"host", "database", "application", "knowledge"},
    "uses": {"host", "database", "application"},
}


class DisconnectedDWSAnchors(ValueError):
    pass


def anchor_relationships(graph):
    targets = {item["id"] for item in graph["entities"] if item["kind"] in RELATION_TARGETS["reviews"]}
    return [edge for edge in graph["relations"] if edge["source"] != edge["target"]
            and {edge["source"], edge["target"]} <= targets]


def validate(anchors, node_ids, graph, *, minimum):
    kinds = {item["id"]: item["kind"] for item in graph["entities"]}
    keys = {(edge.node_id, edge.entity_id) for edge in anchors}
    if (len(keys) != len(anchors) or len({target for _, target in keys}) < minimum
            or any(edge.node_id not in node_ids or kinds.get(edge.entity_id) not in RELATION_TARGETS[edge.relation]
                   for edge in anchors)):
        raise ValueError("Invalid DWS entity anchors")
    targets = {target for _, target in keys}
    if minimum > 1 and not any({edge["source"], edge["target"]} <= targets for edge in anchor_relationships(graph)):
        raise DisconnectedDWSAnchors("DWS anchors must include an existing relationship")
    return [edge.model_dump() for edge in anchors]


def validate_claims(claims, graph, targets):
    entities = {item["id"]: item for item in graph["entities"]}
    indexed = {}
    for claim in claims:
        entity = entities.get(claim.entity_id)
        if entity is None or claim.entity_id not in targets or claim.id in indexed:
            raise ValueError("Invalid DWS claim reference")
        if bool(claim.protocol) != (claim.field == "service_version"):
            raise ValueError("Invalid DWS claim protocol")
        if claim.field in ("hosted_on", "supports"):
            destinations = {edge["target"] for edge in graph["relations"]
                            if edge["source"] == claim.entity_id and edge["relation"] == claim.field}
            destination = entities.get(next(iter(destinations))) if len(destinations) == 1 else None
            expected = destination and destination["label"]
        elif claim.field == "service_version":
            expected = entity.get("service_versions", {}).get(claim.protocol)
        else:
            expected = entity.get(claim.field)
        if expected != claim.value:
            raise ValueError("DWS claim contradicts the baseline")
        indexed[claim.id] = claim
    return indexed


def validate_text(text, claims, *, labels=()):
    for match in SLOT.finditer(text):
        if match[1] not in claims:
            raise ValueError("Unknown DWS claim slot")
    for match in QUALIFIED_SLOT.finditer(text):
        qualifier = match["qualifier"].lower()
        if qualifier in {"hosted on", "running on", "deployed on", "主机", "宿主",
                         "部署于", "部署在", "运行于", "运行在", "托管于", "托管在"}:
            qualifier = "hosted_on"
        if qualifier in {"权限范围", "access scope"}:
            qualifier = "access_scope"
        if qualifier == "世界版本":
            qualifier = "world_version"
        if qualifier == "引擎版本":
            qualifier = "version"
        if claims[match[2]].field not in CRITICAL_FIELDS.get(qualifier, set()):
            raise ValueError("Use a matching DWS claim for infrastructure prose")
    scrubbed = SLOT.sub("", QUALIFIED_SLOT.sub("", text))
    if "[[dws:" in scrubbed or CRITICAL_PROSE.search(scrubbed):
        raise ValueError("Use structured DWS claims for infrastructure facts")
    # Allow exact anchored labels as names in ordinary prose, after checking
    # that they weren't used to make an infrastructure assignment.
    unexplained = scrubbed
    for label in labels:
        unexplained = re.sub(rf"(?<![A-Za-z0-9_-]){re.escape(label)}(?![A-Za-z0-9_-])", "", unexplained, flags=re.I)
    if INFRA_NAME.search(unexplained):
        raise ValueError("Use structured DWS claims for infrastructure facts")


def validate_data(data, claims, *, labels=(), entity_kinds=None):
    if isinstance(data, dict):
        for key, value in data.items():
            normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower().replace("-", "_")
            if normalized in CRITICAL_FIELDS:
                match = SLOT.fullmatch(value) if isinstance(value, str) else None
                claim = claims.get(match[1]) if match else None
                if claim is None or claim.field not in CRITICAL_FIELDS[normalized]:
                    raise ValueError("Use a matching DWS claim for infrastructure fields")
                if claim.field == "label":
                    kind = {"host": "host", "hostname": "host", "server": "host", "db_host": "host",
                            "application": "application", "database": "database"}.get(normalized)
                    if kind and (entity_kinds or {}).get(claim.entity_id) != kind:
                        raise ValueError("DWS claim has the wrong entity type")
            validate_data(value, claims, labels=labels, entity_kinds=entity_kinds)
    elif isinstance(data, list):
        for value in data:
            validate_data(value, claims, labels=labels, entity_kinds=entity_kinds)
    elif isinstance(data, str):
        validate_text(data, claims, labels=labels)


def materialize_scene(scene):
    """Substitute the approved facts at the rendering boundary."""
    values = {claim["id"]: claim["value"] for claim in scene.get("fact_claims", ())}
    if not values:
        return scene

    def substitute(value):
        if isinstance(value, str):
            return SLOT.sub(lambda match: values[match[1]], value)
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        if isinstance(value, list):
            return [substitute(item) for item in value]
        return value

    result = {key: substitute(scene[key]) if key in ("title", "objective") else scene[key] for key in scene}
    result["nodes"] = [
        {key: substitute(value) if key in ("title", "body", "lure_instruction") else value
         for key, value in node.items()} for node in scene["nodes"]
    ]
    result["services"] = [dict(service, data=substitute(service["data"])) for service in scene.get("services", ())]
    return result


def protocol_graph(world, version, entry):
    graph = project(world, version)
    protocol = {"ssh": "ssh", "mysql": "mysql", "llm": "openai"}[entry]
    hosts = {entity["id"] for entity in graph["entities"] if protocol in entity.get("protocols", [])}
    hosted = {edge["source"] for edge in graph["relations"]
              if edge["relation"] == "hosted_on" and edge["target"] in hosts}
    primary = hosts | hosted
    relations = [edge for edge in graph["relations"] if {edge["source"], edge["target"]} & primary]
    neighbors = {node for edge in relations for node in (edge["source"], edge["target"])} - primary
    return {
        "entities": [entity for entity in graph["entities"] if entity["id"] in primary]
                    + [entity for entity in graph["entities"] if entity["id"] in neighbors],
        "primary_entity_ids": sorted(primary),
        "relations": relations,
    }


def related_context(graph, scene, events, limit=12):
    """Focus the existing authorized graph without losing already published anchors."""
    anchors = (scene or {}).get("entity_links", ())
    recent_nodes = {event["what"].get("node_id") for event in events}
    focus = {edge["entity_id"] for edge in anchors if edge["node_id"] in recent_nodes}
    if not focus:
        return graph
    required = {edge["entity_id"] for edge in anchors}
    neighbors = {entity for edge in graph["relations"] if {edge["source"], edge["target"]} & focus
                 for entity in (edge["source"], edge["target"])}
    nearby = [entity for entity in graph["entities"] if entity["id"] in focus | neighbors | required]
    other = [entity for entity in graph["entities"] if entity["id"] not in focus | neighbors | required]
    entities = nearby + other[:max(0, limit - len(nearby))]
    ids = {entity["id"] for entity in entities}
    return {"entities": entities,
            "relations": [edge for edge in graph["relations"] if {edge["source"], edge["target"]} <= ids]}
