"""Expiring, destination-scoped synthetic credentials derived from delivery and use receipts."""

import hashlib
import hmac
import json
import os
import secrets
from base64 import urlsafe_b64encode
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time


DEFAULT_ROUTE = ("web", "ssh", "mysql", "llm", "web")
ALLOWED_HOPS = {"web": {"ssh", "mysql", "llm"}, "ssh": {"mysql"},
                "mysql": {"llm"}, "llm": {"web"}}
TTL = 600
# Web -> SSH -> MySQL -> simulated AI -> final Web verification.
MAX_DEPTH = 4


def enabled():
    return os.environ.get("HONEYNET_CANARY_HANDOFFS") == "1"


def route_for(db, run):
    row = db.execute("SELECT route FROM handoff_policies WHERE run=?", (run,)).fetchone()
    return tuple(json.loads(row[0])) if row else DEFAULT_ROUTE


def set_route(db, run, route):
    route = tuple(route)
    if (not 1 <= len(route) <= MAX_DEPTH + 1 or route[0] != "web"
        or len(set(route[1:])) != len(route[1:])
        or ("web" in route[1:-1])
        or any(target not in ALLOWED_HOPS.get(source, ()) for source, target in zip(route, route[1:]))):
        raise ValueError("Unsupported Canary path")
    db.execute("INSERT OR REPLACE INTO handoff_policies VALUES (?,?)", (run, json.dumps(route)))
    return route


def _locked_route(db, root, run, *, assign=False):
    row = db.execute("SELECT route FROM handoff_routes WHERE root=?", (root,)).fetchone()
    if row:
        return tuple(json.loads(row[0]))
    if not assign:
        return DEFAULT_ROUTE  # Existing in-flight Canaries predate path configuration.
    route = route_for(db, run)
    db.execute("INSERT INTO handoff_routes VALUES (?,?)", (root, json.dumps(route)))
    return route


def _sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _grant(db, identifier):
    return db.execute("SELECT root_id,target_entry,depth FROM canary_handoffs WHERE id=?",
                      (identifier,)).fetchone()


def _live(lab, db, identifier):
    credential = lab._canary(db, identifier)
    return credential if lab._canary_status(credential, int(time())) == "active" else None


def _eligible(lab, db, identifier, entry, allow_root=False):
    grant = _grant(db, identifier)
    credential = _live(lab, db, identifier)
    if credential is None or not db.execute(
        "SELECT 1 FROM lure_response_receipts WHERE token_hash=?", (identifier,)
    ).fetchone():
        return None
    if grant is not None:
        root_id, target, depth = grant
        if target != entry or _live(lab, db, root_id) is None:
            return None
    elif not allow_root or credential["scope"] != "source_web_session":
        return None
    else:
        root_id, depth = identifier, 0
    return credential, root_id, depth


def directed(db, identifier):
    return _grant(db, identifier) is not None


def _describe(entry, token, run):
    if entry == "ssh":
        return {"entry": entry, "username": "canary-check", "password": token}
    if entry == "mysql":
        return {"entry": entry, "username": "canary_" + _sha(token)[:24], "password": token}
    if entry == "llm":
        return {"entry": entry, "verification_key": token}
    return {"entry": "web", "href": f"/decoy/{run}/beacon/{token}"}


def prove(lab, db, parent, entry, session, *, allow_root=False, ssh_material=None):
    """Record valid use and reserve a child. The child is unusable until its response receipt."""
    eligible = _eligible(lab, db, parent, entry, allow_root=allow_root)
    if eligible is None:
        return False, None
    credential, root, depth = eligible
    if depth and db.execute("SELECT 1 FROM lure_uses WHERE token_hash=? AND session<>?",
                            (parent, session)).fetchone():
        return False, None
    db.execute("INSERT OR IGNORE INTO lure_uses(token_hash,entry,session) VALUES (?,?,?)",
               (parent, entry, session))
    use = db.execute("SELECT id FROM lure_uses WHERE token_hash=? AND entry=? AND session=?",
                     (parent, entry, session)).fetchone()[0]
    run = credential["deployment_id"]
    route = _locked_route(db, root, run)
    if depth >= len(route) or entry != route[depth]:
        raise ValueError("Canary path and credential scope disagree")
    if depth == len(route) - 1:
        return True, None
    old = db.execute("SELECT id,parent_use,target_entry,token FROM canary_handoffs WHERE parent_id=? AND source_entry=?",
                     (parent, entry)).fetchone()
    if old is not None:
        if old[1] != use or _live(lab, db, old[0]) is None or ssh_material not in (None, old[3]):
            return True, None
        return True, (old[0], _describe(old[2], old[3], run))
    target = route[depth + 1]
    if target == "ssh" and not os.environ.get("HONEYNET_COWRIE_EXPORT"):
        return True, None
    token = ssh_material or secrets.token_urlsafe(24)
    identifier = _sha(token)
    expires = min(credential["expires_at"], int(time()) + TTL)
    db.execute("INSERT INTO canary_credentials VALUES (?,?,?,?,?,?,?,?,NULL)", (
        identifier, run, session, f"handoff/{use}", credential["world_version"],
        "canary_handoff", f"entry:{target}", expires,
    ))
    db.execute("INSERT INTO canary_handoffs VALUES (?,?,?,?,?,?,?,?,?)", (
        identifier, parent, use, root, entry, target, depth + 1, token,
        secrets.token_hex(32) if target == "ssh" else None,
    ))
    return True, (identifier, _describe(target, token, run))


def _delivered(lab, db, identifier):
    row = db.execute("SELECT target_entry,ssh_nonce,root_id,depth FROM canary_handoffs WHERE id=?",
                     (identifier,)).fetchone()
    credential = _live(lab, db, identifier)
    if row is None or credential is None:
        return
    if row[0] == "ssh":
        projection = os.environ.get("HONEYNET_COWRIE_EXPORT")
        if not projection:
            return
        directory = Path(projection).resolve().parent / "handoffs"
        directory.mkdir(mode=0o755, exist_ok=True)
        directory.chmod(0o755)
        now = int(time())
        for path in directory.glob("*.json"):
            if path.stat().st_mtime < now - TTL:
                path.unlink(missing_ok=True)
        with NamedTemporaryFile(mode="w", dir=directory, delete=False, encoding="utf-8") as stream:
            path = Path(stream.name)
            try:
                route = _locked_route(db, row[2], credential["deployment_id"])
                json.dump({"nonce": row[1], "expires": credential["expires_at"],
                           "terminal": row[3] == len(route) - 1}, stream)
                stream.flush()
                path.chmod(0o644)
                path.replace(directory / (identifier + ".json"))
            finally:
                path.unlink(missing_ok=True)
    db.execute("INSERT OR IGNORE INTO lure_response_receipts(token_hash) VALUES (?)", (identifier,))


def remove_revoked_ssh_permits(db, when):
    projection = os.environ.get("HONEYNET_COWRIE_EXPORT")
    if not projection:
        return
    directory = Path(projection).resolve().parent / "handoffs"
    for identifier, in db.execute("SELECT h.id FROM canary_handoffs h JOIN canary_credentials c "
                                  "ON c.credential_id=h.id WHERE h.target_entry='ssh' AND c.revoked_at=?", (when,)):
        try:
            (directory / (identifier + ".json")).unlink(missing_ok=True)
        except OSError:
            # The ledger and every downstream verifier still fail closed.
            pass


def delivered(lab, identifier):
    with lab.connect() as db:
        _delivered(lab, db, identifier)


def permits_for(lab, identifier):
    with lab.connect() as db:
        row = _grant(db, identifier)
        return row is not None and _live(lab, db, identifier) is not None and _live(lab, db, row[0]) is not None


def ssh_token(nonce, parent, transport):
    material = f"ssh:{parent}:{transport}".encode()
    return urlsafe_b64encode(hmac.digest(bytes.fromhex(nonce), material, "sha256")[:24]).decode()


def ssh_receipt(lab, node, transport, parent, child):
    with lab.connect() as db:
        row = db.execute("SELECT ssh_nonce,root_id,depth FROM canary_handoffs WHERE id=? AND target_entry='ssh'",
                         (parent,)).fetchone()
        if not row:
            return False
        token = ssh_token(row[0], parent, transport)
        if not hmac.compare_digest(_sha(token), child):
            return False
        accepted, reserved = prove(lab, db, parent, "ssh", f"{node}/{transport}", ssh_material=token)
        if reserved is None:
            credential = lab._canary(db, parent)
            return accepted and row[2] == len(_locked_route(db, row[1], credential["deployment_id"])) - 1
        _delivered(lab, db, reserved[0])
        return True


def accept(lab, identifier, entry, session):
    with lab.connect() as db:
        return prove(lab, db, identifier, entry, session)
