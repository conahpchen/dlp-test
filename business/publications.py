"""Approval queue for bounded synthetic documents applied by the DWS writer."""

import re
import secrets
import sqlite3
from contextlib import closing, contextmanager

from pydantic import BaseModel, ConfigDict, Field

from business.atomic_state import read
from business.storage import location
from dws.ids import entity_uuid
from dws.world import CommitRejected
from generation.guard import leak_guard

MAX_PUBLICATIONS = 500
DOCUMENT_PATH = r"^/kb/[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,78}\.md$"


class DocumentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(pattern=DOCUMENT_PATH)
    content: str = Field(min_length=1, max_length=4096)
    base_version: int = Field(ge=1)


@contextmanager
def queue():
    path = location()
    if path is None or path.suffix != ".sqlite" or not path.is_file():
        raise RuntimeError("Initialize the atomic DWS checkpoint to enable publications")
    with closing(sqlite3.connect(path.with_name("proposals.sqlite"), timeout=5)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS proposals (
            id TEXT PRIMARY KEY, path TEXT NOT NULL, content TEXT NOT NULL,
            base_version INTEGER NOT NULL, status TEXT NOT NULL, result_version INTEGER,
            error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        yield db


def _get(db, identifier):
    row = db.execute("SELECT * FROM proposals WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise KeyError(identifier)
    return dict(row)


def current_documents():
    path = location()
    if path is None or path.suffix != ".sqlite" or not path.is_file():
        raise RuntimeError("Initialize the atomic DWS checkpoint to enable publications")
    state = read(path)
    private = set(state.get("private_versions", ()))
    documents = []
    for versions in state["facts"]["data"].values():
        fact = next((row for row in reversed(versions) if row["world_version"] not in private), None)
        if fact is None:
            continue
        document = fact["payload"]
        if (document["kind"] == "file" and not document["canary"] and not document["deleted"]
                and re.fullmatch(DOCUMENT_PATH, document["path"])):
            documents.append({"path": document["path"], "content": document.get("content") or ""})
    return {"world_version": state["meta"]["world_version"],
            "documents": sorted(documents, key=lambda document: document["path"])}


def list_proposals():
    with queue() as db:
        return [dict(row) for row in db.execute("SELECT * FROM proposals ORDER BY rowid DESC")]


def submit(proposal):
    with queue() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT count(*) FROM proposals").fetchone()[0] >= MAX_PUBLICATIONS:
            raise ValueError("Publication queue capacity reached")
        identifier = secrets.token_hex(16)
        db.execute("INSERT INTO proposals(id,path,content,base_version,status) VALUES (?,?,?,?,'draft')",
                   (identifier, proposal.path, proposal.content, proposal.base_version))
        return _get(db, identifier)


def request_transition(identifier, action):
    source, target = {"approve": ("draft", "approved"), "rollback": ("active", "rollback_pending")}[action]
    with queue() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _get(db, identifier)
        if row["status"] in (target, "active" if action == "approve" else "rolled_back"):
            return row
        if row["status"] != source:
            raise ValueError("Publication state does not permit this operation")
        db.execute("UPDATE proposals SET status=?,error=NULL WHERE id=?", (target, identifier))
        return _get(db, identifier)


def _apply(engine, row, checkpoint):
    world = engine.world
    view = engine.session_view()
    releases = world.meta.get("document_publications", {})
    identifier = row["id"]
    operation = "rollback" if row["status"] == "rollback_pending" else "approve"
    previous = releases.get(identifier, {})
    if operation in previous:
        return previous[operation]

    entity_id = entity_uuid("data", row["path"])
    current = view.current("data", entity_id)
    if operation == "rollback":
        if current is None or current.fact_id != previous.get("fact_id"):
            raise ValueError("Document has changed since this proposal was activated")
        historical = world.view(previous["base_version"]).current("data", entity_id)
        payload = dict(historical.payload) if historical else dict(current.payload, deleted=True, content="")
    else:
        if row["base_version"] > view.pinned:
            raise ValueError("Proposal base version is unavailable")
        if leak_guard(row["content"].splitlines(), world):
            raise ValueError("Document failed the DWS content boundary")
        original = world.view(row["base_version"]).current("data", entity_id)
        if (original.fact_id if original else None) != (current.fact_id if current else None):
            raise ValueError("Document has changed since the proposal was drafted")
        if current and (current.payload["kind"] != "file" or current.payload["canary"]):
            raise ValueError("Only regular synthetic documents can be published")
        now = world.meta["now_hlc"]
        owner = entity_uuid("identity", "root")
        if not current and view.current("identity", owner) is None:
            raise ValueError("The DWS requires a root owner for new documents")
        payload = dict(current.payload) if current else dict(
            path=row["path"], owner_entity_id=owner, ctime_hlc=now, kind="file", canary=False,
        )
        payload.update(content=row["content"], mtime_hlc=now, deleted=False)

    engine.commit_external(None, [("data", payload)], actor_id="publication:" + identifier,
                           kind="world_publication")
    version = world.meta["world_version"]
    record = dict(previous, **{operation: version})
    if operation == "approve":
        record.update(fact_id=engine.session_view().current("data", entity_id).fact_id,
                      base_version=row["base_version"])
    world.meta["document_publications"] = dict(releases, **{identifier: record})
    checkpoint(engine)
    return version


def apply_next(engine, checkpoint):
    """Called under the existing DWS writer lock, with durable retry recovery."""
    with queue() as db:
        row = db.execute("SELECT * FROM proposals WHERE status IN ('approved','rollback_pending') "
                         "ORDER BY rowid LIMIT 1").fetchone()
    if row is None:
        return
    try:
        version = _apply(engine, row, checkpoint)
        status = "rolled_back" if row["status"] == "rollback_pending" else "active"
        error = None
    except (ValueError, CommitRejected) as exc:
        status, version, error = "rejected", None, str(exc)
    with queue() as db:
        db.execute("UPDATE proposals SET status=?,result_version=?,error=? WHERE id=? AND status=?",
                   (status, version, error, row["id"], row["status"]))
