"""Explicit, expiring projection bindings. A binding conveys no account privileges."""

import hashlib
import os
from pathlib import Path
import re
import secrets
from time import time


HEADER = "x-honeynet-binding"
MYSQL_PREFIX = "catalog_"
TTL = 3600
ENTRIES = frozenset({"web", "ssh", "mysql", "llm"})


def _key(token):
    if not re.fullmatch(r"[a-f0-9]{24}", token):
        raise KeyError("Unknown binding")
    return hashlib.sha256(token.encode()).hexdigest()


def export_ssh_binding(plan, binding_id, expires_at, branch_version=None):
    from business.storage import location, restore
    from dws.world import World
    from entries.cowrie.projection import prepare, write_snapshot
    from replay.engine import Engine

    projection = os.environ.get("HONEYNET_COWRIE_EXPORT")
    if not projection:
        return False
    directory = Path(projection).resolve().parent
    checkpoint = location()
    if not checkpoint or directory == checkpoint.resolve().parent:
        raise ValueError("SSH bindings require a dedicated projection directory")
    engine = Engine(World({"meta": {"seed": "binding-reader"}}))
    if restore(engine) is None or engine.world.meta["world_id"] != plan["world_id"]:
        raise ValueError("DWS binding source is unavailable")
    snapshot = prepare(engine.world, os.environ.get("HONEYNET_COWRIE_HOST", "web01"),
                       version=plan["world_version"], branch_version=branch_version)
    snapshots = directory / "bindings"
    snapshots.mkdir(mode=0o755, exist_ok=True)
    snapshots.chmod(0o755)
    now = int(time())
    for path in snapshots.glob("*.json"):
        # Expiration is also enforced when accepting an SSH request.
        if path.stat().st_mtime < now - TTL:
            path.unlink(missing_ok=True)
    write_snapshot(dict(snapshot, expires_at=expires_at), snapshots / (binding_id + ".json"))
    return True


def issue(lab, run, publication=None, *, target=None):
    if target is not None and target not in ENTRIES:
        raise ValueError("Unknown binding target")
    token = secrets.token_hex(12)
    with lab.connect() as db:
        plan = lab._plan(db, run)
        if not db.execute("SELECT 1 FROM release_manifests WHERE run=?", (run,)).fetchone():
            raise ValueError("A binding requires a prepared release")
        db.execute("DELETE FROM version_bindings WHERE expires_at<=?", (int(time()),))
        db.execute("DELETE FROM binding_overlays WHERE id NOT IN (SELECT id FROM version_bindings)")
        db.execute("DELETE FROM binding_targets WHERE id NOT IN (SELECT id FROM version_bindings)")
        if db.execute("SELECT count(*) FROM version_bindings").fetchone()[0] >= 500:
            raise ValueError("Binding capacity reached")
        binding_id = _key(token)
        expires_at = int(time()) + TTL
        branch_version = None
        if publication is not None:
            from business.graph_publications import load_branch

            branch_version = load_branch(publication, plan)
        ssh = target in (None, "ssh") and export_ssh_binding(plan, binding_id, expires_at, branch_version)
        if target == "ssh" and not ssh:
            raise ValueError("SSH projection is unavailable")
        db.execute("INSERT INTO version_bindings VALUES (?,?,?,?,?)",
                   (binding_id, run, plan["world_id"], plan["world_version"], expires_at))
        if branch_version is not None:
            db.execute("INSERT INTO binding_overlays VALUES (?,?)", (binding_id, branch_version))
        if target is not None:
            db.execute("INSERT INTO binding_targets VALUES (?,?)", (binding_id, target))
    result = {"binding": token, "mysql_user": MYSQL_PREFIX + token if target in (None, "mysql") else None, "run": run,
              "world_version": plan["world_version"], "ssh_environment": "HONEYNET_BINDING" if ssh else None}
    if target is not None:
        result["target"] = target
    if branch_version is not None:
        result["branch_version"] = branch_version
    return result


def resolve(lab, token, *, entry):
    binding_id = _key(token)
    with lab.connect() as db:
        row = db.execute("SELECT v.run,v.world_id,v.world_version,b.branch_version FROM version_bindings v "
                         "LEFT JOIN binding_overlays b ON b.id=v.id "
                         "LEFT JOIN binding_targets t ON t.id=v.id "
                         "WHERE v.id=? AND v.expires_at>? AND (t.entry IS NULL OR t.entry=?)",
                         (binding_id, int(time()), entry)).fetchone()
    if row is None:
        raise KeyError("Unknown binding")
    result = {"id": binding_id, "run": row[0], "world_id": row[1], "world_version": row[2]}
    if row[3] is not None:
        result["branch_version"] = row[3]
    return result
