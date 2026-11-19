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


def _key(token):
    if not re.fullmatch(r"[a-f0-9]{24}", token):
        raise KeyError("Unknown binding")
    return hashlib.sha256(token.encode()).hexdigest()


def export_ssh_binding(plan, binding_id, expires_at):
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
                       version=plan["world_version"])
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


def issue(lab, run):
    token = secrets.token_hex(12)
    with lab.connect() as db:
        plan = lab._plan(db, run)
        if not db.execute("SELECT 1 FROM release_manifests WHERE run=?", (run,)).fetchone():
            raise ValueError("A binding requires a prepared release")
        db.execute("DELETE FROM version_bindings WHERE expires_at<=?", (int(time()),))
        if db.execute("SELECT count(*) FROM version_bindings").fetchone()[0] >= 500:
            raise ValueError("Binding capacity reached")
        binding_id = _key(token)
        expires_at = int(time()) + TTL
        ssh = export_ssh_binding(plan, binding_id, expires_at)
        db.execute("INSERT INTO version_bindings VALUES (?,?,?,?,?)",
                   (binding_id, run, plan["world_id"], plan["world_version"], expires_at))
    return {"binding": token, "mysql_user": MYSQL_PREFIX + token, "run": run,
            "world_version": plan["world_version"], "ssh_environment": "HONEYNET_BINDING" if ssh else None}


def resolve(lab, token):
    binding_id = _key(token)
    with lab.connect() as db:
        row = db.execute("SELECT run,world_id,world_version FROM version_bindings "
                         "WHERE id=? AND expires_at>?", (binding_id, int(time()))).fetchone()
    if row is None:
        raise KeyError("Unknown binding")
    return {"id": binding_id, "run": row[0], "world_id": row[1], "world_version": row[2]}
