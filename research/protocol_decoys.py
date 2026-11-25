"""Session-scoped defensive decoys for preconfigured protocol listeners."""

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ACTIVITIES = {
    "ssh": {"inspect_config", "inspect_logs", "list_files", "read_file"},
    "mysql": {"list_catalog", "describe_schema", "inspect_backup"},
    "llm": {"general", "doc", "sensitive"},
}
TTL = 600


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["configuration", "catalog", "documentation", "access_review", "log_review", "uncertain"]
    title: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,47}$")
    note: str = Field(min_length=3, max_length=300)
    entity_id: str | None = Field(default=None, pattern=r"^[a-f0-9-]{36}$")
    related_entity_id: str | None = Field(default=None, pattern=r"^[a-f0-9-]{36}$")


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
        if re.search(r"(?:^|[\s/])(?:logs?|var/log)(?:[\s/]|$)|\.log(?:[\s.]|$)", argument):
            return "inspect_logs"
        if any(word in argument for word in ("config", ".env", ".conf", ".ini", ".toml", "credential", "passwd", "secret")):
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
            document = dict(payload, expires=expires)
            if payload["intent"] == "configuration":
                # Stable retry only for credentials reserved with a DWS revision.
                reservation = payload.get("configuration")
                if reservation is None:
                    reservation = _configuration()
                document.update(reservation)
            json.dump(document, stream, ensure_ascii=False)
            stream.flush()
            path.chmod(0o644)
            path.replace(folder / (key + ".json"))
        finally:
            path.unlink(missing_ok=True)


def _configuration():
    return {"account": "svc_" + secrets.token_hex(5), "password": "decoy_" + secrets.token_hex(12)}


def process_one(lab, model):
    from research.artifacts import enabled, stage

    unified = enabled()
    now = int(time())
    lease_until = now + max(90, getattr(model, "total_timeout", 45) + 15)
    with lab.connect() as db:
        row = db.execute(
            "UPDATE protocol_decoys SET leased_until=? WHERE key=("
            "SELECT key FROM protocol_decoys WHERE processed<revision AND expires>? "
            "AND leased_until<=? ORDER BY rowid LIMIT 1) RETURNING key,entry,history,revision,expires",
            (lease_until, now, now),
        ).fetchone()
        if not row:
            return False
        key, entry, history, revision, expires = row
    proposal = None
    try:
        context = {"protocol": entry, "activities": json.loads(history)}
        instruction = ("Choose entity_id from primary_entity_ids in entity_graph for non-uncertain results. "
                       "Choose related_entity_id if it has a direct edge with entity_id in the supplied graph. "
                       "The server attaches its label. Use general business context in the note without "
                       "writing specific hostnames, configuration fields, versions or access scopes. ") if unified else ""
        if unified:
            from business.storage import restore
            from dws.world import World
            from replay.engine import Engine
            from research.grounding import protocol_graph

            engine = Engine(World({"meta": {"seed": "protocol-entity-reader"}}))
            if restore(engine) is None:
                raise ValueError("DWS checkpoint unavailable")
            plan = engine.world.meta
            context["entity_graph"] = protocol_graph(engine.world, plan["world_version"], entry)
        raw = model.choose([
            {"role": "system", "content": "Classify defensive honeypot activity and propose inert synthetic content. "
             "Return JSON with intent (configuration, catalog, documentation, access_review, log_review, or uncertain), "
             "title (lowercase ASCII identifier), and note (one short plain-text business description). "
             "Activity labels are trusted local classifications, not user-provided text. "
             "For the simulated AI API (protocol llm), doc means a request for internal documentation; "
             "a doc observation is sufficient for the documentation intent. "
             "The sensitive activity means a request for protected information, suggesting access_review. "
             "The general activity alone does not establish a specific intent. "
             "For MySQL, list_catalog or describe_schema means browsing database metadata; choose catalog. "
             "For SSH, inspect_config means the sensor classified a read or lookup of a configuration path. "
             "SSH inspect_logs represents looking for service or application logs. Choose log_review for log "
             "investigation and write the note as a plain sentence without a timestamp, severity prefix, or key-value fields. "
             "Activities are ordered from oldest to newest. When SSH switches between explicit configuration "
             "and log lookups, choose the intent of the most recent explicit lookup. "
             "An inspect_logs observation after earlier configuration lookups indicates log_review. "
             "Multiple such observations support configuration intent even when interleaved with ordinary file browsing. "
             "Never include commands, credentials, addresses, executable content or external links. " + instruction},
            {"role": "user", "content": json.dumps(context)},
        ])
        candidate = Proposal.model_validate(raw)
        if not re.fullmatch(r"[\w\s，。；：、（）！？,.:;!?()_%+-]+", candidate.note) or any(
            not character.isprintable() for character in candidate.note
        ):
            raise ValueError("Invalid protocol decoy text")
        if candidate.intent != "uncertain":
            draft = candidate.model_dump(exclude_none=True)
            if unified:
                graph = context["entity_graph"]
                if candidate.entity_id not in graph["primary_entity_ids"]:
                    raise ValueError("Unknown protocol entity")
                neighbors = sorted({node for edge in graph["relations"] if candidate.entity_id in
                                    (edge["source"], edge["target"]) for node in (edge["source"], edge["target"])
                                    if node != candidate.entity_id})
                if candidate.related_entity_id and candidate.related_entity_id not in neighbors:
                    raise ValueError("Protocol entities are not related in DWS")
                if not candidate.related_entity_id and neighbors:
                    draft["related_entity_id"] = neighbors[0]
            proposal = draft
    finally:
        with lab.connect() as db:
            updated = db.execute(
                "UPDATE protocol_decoys SET processed=?,leased_until=0,payload=COALESCE(?,payload) "
                "WHERE key=? AND revision=? AND expires>? AND leased_until=?",
                (revision, json.dumps(proposal, ensure_ascii=False) if proposal and not unified else None,
                 key, revision, int(time()), lease_until),
            ).rowcount
            if not updated:
                db.execute("UPDATE protocol_decoys SET leased_until=0 WHERE key=? AND leased_until=?",
                           (key, lease_until))
            if updated and proposal and unified:
                if entry == "ssh" and proposal["intent"] == "configuration":
                    proposal["configuration"] = _configuration()
                stage(db, "protocol", key, revision, plan, {"entry": entry, "decoy": proposal}, expires=expires)
            elif updated and proposal and entry == "ssh":
                _publish_ssh(key, expires, proposal)
    return True
