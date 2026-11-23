"""Publish generated material through the single DWS writer before delivery."""

import hashlib
import json
import os
from time import time

from dws.ids import entity_uuid
from dws.world import CommitRejected
from generation.guard import leak_guard
from research.grounding import materialize_scene, project, protocol_graph, validate_text

ADAPTATION_TTL = 3600

def enabled():
    return os.environ.get("RESEARCH_UNIFIED_PUBLICATION") == "1"


def stage(db, kind, subject, revision, plan, content, *, expires=None):
    # An ongoing protocol connection can create another observation window after TTL.
    epoch = f":{expires}" if kind == "protocol" else ""
    identifier = hashlib.sha256(f"{kind}:{subject}:{revision}{epoch}".encode()).hexdigest()
    db.execute(
        "INSERT OR IGNORE INTO generated_releases VALUES (?,?,?,?,?,?,?,?, 'pending',NULL,NULL)",
        (identifier, kind, subject, revision, plan["world_id"], plan["world_version"],
         json.dumps(content, ensure_ascii=False), expires),
    )
    return identifier


def release(db, identifier):
    return db.execute("SELECT status,world_version,error FROM generated_releases WHERE id=?",
                      (identifier,)).fetchone()


def _scene_graph(world, plan, scene):
    from research.generation import scene_edges, validate_scene

    current = project(world, plan["world_version"], plan["documents"])
    checked = validate_scene(scene, plan["documents"], current, typed_facts=bool(plan.get("fact_schema")))
    rendered = materialize_scene(checked.model_dump())
    pages = {page["id"]: page for page in rendered["nodes"]}
    routes = {route["id"]: route for route in rendered["services"]}
    nodes = [{"id": page.id, "kind": "document", "content": pages[page.id]["body"], "title": pages[page.id]["title"],
              "verification_note": pages[page.id]["lure_instruction"],
              "sources": [source.fact_id for source in page.sources]} for page in checked.nodes]
    for route in checked.services:
        identifier = "http:" + route.id
        nodes.append({"id": identifier, "kind": "http_route", "method": route.method,
                      "path": route.path, "data": routes[route.id]["data"]})
    referenced = {anchor.entity_id for anchor in checked.entity_links}
    nodes.extend({"id": entity["id"], "kind": "dws_entity", "entity_kind": entity["kind"],
                  "label": entity["label"]} for entity in current["entities"] if entity["id"] in referenced)
    return {"nodes": nodes, "edges": scene_edges(checked, current), "capabilities": checked.capability_requests,
            "fact_claims": rendered["fact_claims"],
            "isolated_reason": checked.isolated_reason}


def _artifact(world, kind, content, base):
    if kind == "scene":
        return _scene_graph(world, content["plan"], content["plan"]["scene"])
    if kind == "adaptation":
        return _scene_graph(world, content["plan"], content["policy"]["scene"])
    entry, decoy = content["entry"], content["decoy"]
    node_kind = {"ssh": "ssh_file", "mysql": "mysql_catalog", "llm": "ai_reference"}[entry]
    ground = protocol_graph(world, base, entry)
    entity = next((item for item in ground["entities"] if item["id"] == decoy.get("entity_id")), None)
    if entity is None or entity["id"] not in ground["primary_entity_ids"]:
        raise ValueError("Invalid protocol artifact anchor")
    validate_text(decoy["note"], {}, labels=[item["label"] for item in ground["entities"]])
    selected = {entity["id"]}
    if related := decoy.get("related_entity_id"):
        selected.add(related)
    relations = [dict(source=edge["source"], target=edge["target"], kind=edge["relation"])
                 for edge in ground["relations"] if {edge["source"], edge["target"]} == selected]
    if len(selected) > 1 and not relations:
        raise ValueError("Protocol entity relationship changed")
    nodes = [{"id": decoy["title"], "kind": node_kind, "content": decoy["note"], "intent": decoy["intent"]}]
    nodes.extend(dict(id=row["id"], kind="dws_entity", entity_kind=row["kind"], label=row["label"])
                 for row in ground["entities"] if row["id"] in selected)
    edges = [dict(source=decoy["title"], target=row["id"], kind="documents") for row in nodes[1:]]
    return {"nodes": nodes, "edges": edges + relations}


def _publish(engine, checkpoint, row, graph):
    identifier, kind, subject, revision, world_id, base, _, _ = row
    world = engine.world
    previous = world.meta.get("generated_releases", {})
    if identifier in previous:
        return previous[identifier]
    if world.meta["world_id"] != world_id or not 1 <= base <= world.meta["world_version"]:
        raise ValueError("Invalid DWS baseline")
    for node in graph["nodes"]:
        visitor_fields = {key: node[key] for key in ("title", "content", "data", "verification_note") if key in node}
        if leak_guard([json.dumps(visitor_fields, ensure_ascii=False)], world):
            raise ValueError("Generated content failed the DWS boundary")
    owner_key = _actor(kind, subject)
    owners = world.meta.setdefault("generated_owners", {})
    # Each live generation has a stable entity ID. Reopening an archived subject
    # starts a new entity so its historical fact IDs can never be reused.
    existing = entity_uuid("object", "generated_graph:" + owner_key) in world.facts["object"]
    actor = owners.get(owner_key) or (owner_key if kind == "scene" or existing else f"{owner_key}:{identifier}")
    created = actor not in world.sessions
    world.open_session(actor, actor, None, None)
    try:
        engine.commit_external(actor, [("object", {
            "type": "generated_graph", "object_key": actor, "data": {
                **graph, "kind": kind, "revision": revision, "base": base,
            },
        })], actor_id=actor, kind="generated_graph", private=True)
    except (CommitRejected, ValueError):
        if created:
            world.sessions.pop(actor, None)
        raise
    version = world.meta["world_version"]
    owners[owner_key] = actor
    world.meta["generated_releases"] = dict(previous, **{identifier: version})
    checkpoint(engine)
    return version


def _actor(kind, subject):
    return "generated:" + hashlib.sha256(f"{kind}:{subject}".encode()).hexdigest()


def archive_expired(lab, engine, checkpoint):
    """Retire only acknowledged private releases whose online projection has expired."""
    from business.atomic_state import archived_graph
    from business.storage import location

    path = location()
    if path is None or path.suffix != ".sqlite":
        return
    now = int(time())
    world = engine.world
    with lab.connect() as db:
        db.execute("UPDATE generated_releases SET expires=? WHERE kind='adaptation' AND expires IS NULL",
                   (now + ADAPTATION_TTL,))
        jobs = db.execute(
            "SELECT g.id,g.kind,g.subject,g.world_version FROM generated_releases g "
            "WHERE g.status='active' AND g.kind IN ('adaptation','protocol') AND g.expires<=? "
            "AND NOT EXISTS (SELECT 1 FROM binding_overlays b JOIN version_bindings t ON t.id=b.id "
            "WHERE b.branch_version=g.world_version AND t.expires_at>?) ORDER BY g.expires",
            (now, now),
        ).fetchall()
        if not jobs:
            return
        published = world.meta.get("generated_releases", {})
        owners = world.meta.get("generated_owners", {})
        eligible = {version: identifier for identifier, _, _, version in jobs}
        subjects = dict.fromkeys((kind, subject) for _, kind, subject, _ in jobs)
        archived, acknowledged = [], []
        for kind, subject in subjects:
            owner_key = _actor(kind, subject)
            actor = owners.get(owner_key, owner_key)
            entity = entity_uuid("object", "generated_graph:" + actor)
            history = world.facts["object"].get(entity, [])
            if (not history or any(
                fact.world_version not in world.private_versions or fact.world_version not in eligible
                or published.get(eligible[fact.world_version]) != fact.world_version for fact in history
            )):
                continue
            for fact in history:
                archived.append(fact.to_row())
                identifier = eligible[fact.world_version]
                published.pop(identifier)
                acknowledged.append((identifier,))
            world.facts["object"].pop(entity)
            world.sessions.pop(actor, None)
            owners.pop(owner_key, None)
            if len(archived) >= 32:
                break
        # The DWS transaction can have committed before the research ledger.
        newly_archived = {fact["world_version"] for fact in archived}
        for identifier, _, _, version in jobs:
            if version not in newly_archived and identifier not in published and archived_graph(path, version):
                acknowledged.append((identifier,))
        if archived:
            checkpoint(engine, archived=archived)
        db.executemany("UPDATE generated_releases SET status='archived' WHERE id=?", acknowledged)


def live_policy(db, row, *, refresh=False):
    if row is None:
        return {}
    policy = json.loads(row[0])
    if version := policy.get("dws_version"):
        now = int(time())
        release = db.execute("SELECT expires,status FROM generated_releases "
                             "WHERE world_version=? AND kind='adaptation'", (version,)).fetchone()
        if release:
            expires, status = release
            if status == "archived" or expires is not None and expires <= now:
                return {}
            if refresh and expires is not None and expires - now < ADAPTATION_TTL // 2:
                db.execute("UPDATE generated_releases SET expires=? WHERE world_version=? AND kind='adaptation'",
                           (now + ADAPTATION_TTL, version))
    return policy


def graph_summary(version, *, run=None):
    from business.atomic_state import archived_graph, read
    from business.storage import location

    path = location()
    if path is None or not path.is_file():
        return None
    state = read(path)
    groups = state["facts"]["object"]
    if run is not None:
        groups = {"scene": groups.get(entity_uuid("object", "generated_graph:" + _actor("scene", run)), [])}
    fact = next((item for rows in groups.values() for item in rows if item["world_version"] == version
                 and item["payload"]["type"] == "generated_graph"), None)
    archived = False
    if fact is None and run is None:
        fact = archived_graph(path, version)
        archived = fact is not None
    if not fact:
        return None
    payload = fact["payload"]["data"]
    fields = ("id", "kind", "entity_kind", "label", "title", "method", "path")
    return {"base": payload["base"], "version": version, "edges": payload["edges"], "archived": archived,
            "nodes": [{key: node[key] for key in fields if key in node} for node in payload["nodes"]]}


def scene_summary(run, version):
    return graph_summary(version, run=run)


def apply_next(lab, engine, checkpoint):
    from business import publication_retry as retry

    with lab.connect() as db:
        retry.init(db)
        row = db.execute("SELECT g.id,g.kind,g.subject,g.revision,g.world_id,g.base,g.payload,g.expires "
                         "FROM generated_releases g LEFT JOIN publication_retry r "
                         "ON r.lane='generated' AND r.job=g.id WHERE g.status='pending' "
                         "AND COALESCE(r.retry_at,0)<=unixepoch() ORDER BY g.rowid LIMIT 1").fetchone()
    if row is None:
        return
    try:
        _apply_release(lab, engine, checkpoint, row)
    except Exception as exc:
        identifier, kind, _, _, _, _, raw, _ = row
        with lab.connect() as db:
            if retry.failure(db, "generated", identifier):
                if kind == "adaptation":
                    try:
                        _install_adaptation(db, json.loads(raw), None)
                    except (ValueError, KeyError, TypeError):
                        pass  # The release status still records the failed repair.
                db.execute("UPDATE generated_releases SET status='failed',error=? WHERE id=?",
                           (type(exc).__name__, identifier))
                retry.clear(db, "generated", identifier)
    else:
        with lab.connect() as db:
            retry.clear(db, "generated", row[0])


def _apply_release(lab, engine, checkpoint, row):
    identifier, kind, subject, revision, _, base, raw, expires = row
    now = int(time())
    if expires is not None and expires <= now:
        with lab.connect() as db:
            if kind == "adaptation":
                _install_adaptation(db, json.loads(raw), None)
            db.execute("UPDATE generated_releases SET status='rejected',error='expired' WHERE id=?", (identifier,))
        return
    content = json.loads(raw)
    try:
        version = _publish(engine, checkpoint, row, _artifact(engine.world, kind, content, base))
    except (CommitRejected, ValueError) as exc:
        version, error = None, type(exc).__name__
    else:
        error = None
    with lab.connect() as db:
        if kind == "adaptation":
            _install_adaptation(db, content, version)
        if kind == "protocol" and version is not None:
            applicable = db.execute("SELECT revision FROM protocol_decoys WHERE key=? AND expires>?",
                                    (subject, int(time()))).fetchone()
            if applicable == (revision,):
                if content["entry"] == "ssh":
                    from research.protocol_decoys import _publish_ssh

                    _publish_ssh(subject, expires, content["decoy"])
                db.execute("UPDATE protocol_decoys SET payload=? WHERE key=?",
                           (json.dumps(dict(content["decoy"], dws_version=version)), subject))
        db.execute("UPDATE generated_releases SET status=?,world_version=?,error=? WHERE id=?",
                   ("active" if version is not None else "rejected", version, error, identifier))


def _install_adaptation(db, content, version):
    run, policy, attempt = content["plan"]["run_id"], content["policy"], content["attempt"]
    session, revision = policy["session"], policy["revision"]
    current = db.execute("SELECT revision FROM policies WHERE run=? AND session=?", (run, session)).fetchone()
    latest = db.execute("SELECT id,payload FROM decisions WHERE run=? AND session=? ORDER BY id DESC LIMIT 1",
                        (run, session)).fetchone()
    if latest is None or latest[0] != attempt:
        return
    audit = json.loads(latest[1])
    if version is None or (current[0] if current else 0) != revision - 1:
        audit.update(status="rejected", error_code="dws_publication_failed")
    else:
        policy["dws_version"] = version
        db.execute("INSERT OR REPLACE INTO policies VALUES (?,?,?,?,?)",
                   (run, session, revision, policy["observed"], json.dumps(policy)))
        audit.update(status="applied", dws_version=version)
    db.execute("UPDATE decisions SET payload=? WHERE id=?", (json.dumps(audit), attempt))
