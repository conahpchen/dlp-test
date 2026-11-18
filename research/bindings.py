"""Explicit, expiring projection bindings. A binding conveys no account privileges."""

import hashlib
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


def issue(lab, run):
    token = secrets.token_hex(12)
    with lab.connect() as db:
        plan = lab._plan(db, run)
        if not db.execute("SELECT 1 FROM release_manifests WHERE run=?", (run,)).fetchone():
            raise ValueError("A binding requires a prepared release")
        db.execute("DELETE FROM version_bindings WHERE expires_at<=?", (int(time()),))
        if db.execute("SELECT count(*) FROM version_bindings").fetchone()[0] >= 500:
            raise ValueError("Binding capacity reached")
        db.execute("INSERT INTO version_bindings VALUES (?,?,?,?,?)",
                   (_key(token), run, plan["world_id"], plan["world_version"], int(time()) + TTL))
    return {"binding": token, "mysql_user": MYSQL_PREFIX + token, "run": run,
            "world_version": plan["world_version"]}


def resolve(lab, token):
    binding_id = _key(token)
    with lab.connect() as db:
        row = db.execute("SELECT run,world_id,world_version FROM version_bindings "
                         "WHERE id=? AND expires_at>?", (binding_id, int(time()))).fetchone()
    if row is None:
        raise KeyError("Unknown binding")
    return {"id": binding_id, "run": row[0], "world_id": row[1], "world_version": row[2]}
