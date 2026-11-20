"""Session-scoped defensive decoys for preconfigured protocol listeners."""

import hashlib
import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ACTIVITIES = {
    "ssh": {"inspect_config", "list_files", "read_file"},
    "mysql": {"list_catalog", "describe_schema", "inspect_backup"},
    "llm": {"general", "doc", "sensitive"},
}
TTL = 600


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["configuration", "catalog", "documentation", "access_review", "uncertain"]
    title: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,47}$")
    note: str = Field(min_length=3, max_length=300)


def session_key(entry, session):
    return hashlib.sha256(f"{entry}:{session}".encode()).hexdigest()


def observe(lab, entry, session, activity):
    if activity not in ACTIVITIES[entry]:
        return
    now = int(time())
    key = session_key(entry, session)
    with lab.connect() as db:
        db.execute("DELETE FROM protocol_decoys WHERE expires <= ?", (now,))
        row = db.execute("SELECT history FROM protocol_decoys WHERE key=?", (key,)).fetchone()
        if row:
            history = json.loads(row[0])
            if history[-1] != activity:
                db.execute("UPDATE protocol_decoys SET history=?, revision=revision+1 WHERE key=?",
                           (json.dumps((history + [activity])[-8:]), key))
        elif db.execute("SELECT COUNT(*) FROM protocol_decoys").fetchone()[0] < 256:
            db.execute("INSERT INTO protocol_decoys(key,entry,history,expires) VALUES (?,?,?,?)",
                       (key, entry, json.dumps([activity]), now + TTL))


def available(lab, entry, session):
    with lab.connect() as db:
        row = db.execute("SELECT payload FROM protocol_decoys WHERE key=? AND expires>? AND payload IS NOT NULL",
                         (session_key(entry, session), int(time()))).fetchone()
    return json.loads(row[0]) if row else None


def ssh_activity(command):
    verb, _, argument = (command or "").strip().lower().partition(" ")
    if verb in {"ls", "find", "cat", "less", "head", "tail", "more", "grep"}:
        if any(word in argument for word in ("config", ".env", "credential", "passwd", "secret")):
            return "inspect_config"
        return "list_files" if verb in {"ls", "find"} else "read_file"
    return None


def _publish_ssh(key, expires, payload):
    export = os.environ.get("HONEYNET_COWRIE_EXPORT")
    if not export:
        return
    folder = Path(export).resolve().parent / "session-decoys"
    folder.mkdir(mode=0o755, exist_ok=True)
    folder.chmod(0o755)
    now = int(time())
    for file in folder.glob("*.json"):
        if file.stat().st_mtime <= now - TTL:
            file.unlink(missing_ok=True)
    with NamedTemporaryFile(mode="w", dir=folder, delete=False, encoding="utf-8") as stream:
        path = Path(stream.name)
        try:
            json.dump(dict(payload, expires=expires), stream, ensure_ascii=False)
            stream.flush()
            path.chmod(0o644)
            path.replace(folder / (key + ".json"))
        finally:
            path.unlink(missing_ok=True)


def process_one(lab, model):
    now = int(time())
    with lab.connect() as db:
        row = db.execute(
            "UPDATE protocol_decoys SET leased_until=? WHERE key=("
            "SELECT key FROM protocol_decoys WHERE processed<revision AND expires>? "
            "AND leased_until<=? ORDER BY rowid LIMIT 1) RETURNING key,entry,history,revision,expires",
            (now + 90, now, now),
        ).fetchone()
        if not row:
            return False
        key, entry, history, revision, expires = row
    proposal = None
    try:
        raw = model.choose([
            {"role": "system", "content": "Classify defensive honeypot activity and propose inert synthetic content. "
             "Return JSON with intent (configuration, catalog, documentation, access_review, or uncertain), "
             "title (lowercase ASCII identifier), and note (one short plain-text business description). "
             "Never include commands, credentials, addresses, executable content or external links."},
            {"role": "user", "content": json.dumps({"protocol": entry, "activities": json.loads(history)})},
        ])
        candidate = Proposal.model_validate(raw)
        if not re.fullmatch(r"[\w\s，。；：、（）,.()_-]+", candidate.note) or any(
            not character.isprintable() for character in candidate.note
        ):
            raise ValueError("Invalid protocol decoy text")
        if candidate.intent != "uncertain":
            proposal = candidate.model_dump()
    finally:
        with lab.connect() as db:
            updated = db.execute(
                "UPDATE protocol_decoys SET processed=?,leased_until=0,payload=COALESCE(?,payload) "
                "WHERE key=? AND revision=? AND expires>? AND leased_until=?",
                (revision, json.dumps(proposal, ensure_ascii=False) if proposal else None,
                 key, revision, int(time()), now + 90),
            ).rowcount
            if not updated:
                db.execute("UPDATE protocol_decoys SET leased_until=0 WHERE key=? AND leased_until=?",
                           (key, now + 90))
            if updated and proposal and entry == "ssh":
                _publish_ssh(key, expires, proposal)
    return True
