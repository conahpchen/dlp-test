"""Control-plane interpretation of bounded, aggregate honeynet observations."""

import json
import os
import sqlite3
from contextlib import closing
from time import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from audit.situations import WINDOW, _stamp
from research.model import Model

ANALYSES_PER_HOUR = 4
ENTRIES = {"web", "ssh", "mysql", "llm"}
SIGNALS = {"connection", "login", "login_failed", "rate_limited", "session_ended"}
POLICY = """Analyze this defensive honeynet snapshot. Input contains aggregate counts only.
Return a JSON object {assessment, severity, recommendations: [{action, evidence}]}.
assessment is quiet, probing, credential_activity, cross_protocol, or inconclusive.
severity is low, medium, or high. action is observe, notify_operator, or review_block.
Each evidence array contains exact observation IDs from the input. Use cross_protocol
only for a correlated Canary chain. A connection or successful login can be ordinary.
Treat a Canary chain as evidence of reuse, with no claim about a person's identity.
review_block requests an operator review and needs a chain, rate limit, or ended session.
When evidence is limited, prefer inconclusive and observe. No prose, identifiers, targets,
commands, instructions to an operator, or new actions. Return at most three recommendations."""


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["observe", "notify_operator", "review_block"]
    evidence: list[str] = Field(min_length=1, max_length=4)


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    assessment: Literal["quiet", "probing", "credential_activity", "cross_protocol", "inconclusive"]
    severity: Literal["low", "medium", "high"]
    recommendations: list[Recommendation] = Field(min_length=1, max_length=3)


def observations(report):
    evidence = {f"alert:{item['entry']}:{item['kind']}": item["count"] for item in report["alerts"] or []
                if item["entry"] in ENTRIES and item["kind"] in SIGNALS}
    evidence.update({f"canary:{entry}": count for entry, count in (report["canary_matches"] or {}).items()
                     if entry in ENTRIES})
    if report["kind"] == "cross_protocol":
        evidence["chain:protocols"] = report["target_count"]
    return evidence


def assess_next(model_factory=Model, now=None):
    destination = os.environ.get("HONEYNET_SITUATION_REPORT_FILE")
    if not destination:
        return False
    now = int(time()) if now is None else now
    with closing(sqlite3.connect(destination, timeout=4)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS situation_analysis ("
                   "report_id INTEGER PRIMARY KEY, started INTEGER NOT NULL, payload TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM situation_analysis WHERE started<? "
                   "AND report_id NOT IN (SELECT id FROM situation_reports)", (now - 2 * WINDOW,))
        db.execute("UPDATE situation_analysis SET payload='{\"status\":\"unavailable\"}' "
                   "WHERE started<=? AND json_extract(payload,'$.status')='pending'", (now - 60,))
        count = db.execute("SELECT COUNT(*) FROM situation_analysis WHERE started>=? "
                           "AND json_extract(payload,'$.status')<>'no_evidence'",
                           (now // WINDOW * WINDOW,)).fetchone()[0]
        if count >= ANALYSES_PER_HOUR:
            return False
        row = db.execute("SELECT r.id,r.payload FROM situation_reports r "
                         "LEFT JOIN situation_analysis a ON a.report_id=r.id "
                         "WHERE a.report_id IS NULL AND json_extract(r.payload,'$.generated_at')>=? "
                         "ORDER BY json_extract(r.payload,'$.kind')='cross_protocol' DESC,r.id DESC LIMIT 1",
                         (_stamp(now - WINDOW),)).fetchone()
        if row is None:
            return False
        identifier, raw = row
        report = json.loads(raw)
        evidence = observations(report)
        db.execute("INSERT INTO situation_analysis VALUES (?,?,?)",
                   (identifier, now, json.dumps({"status": "pending" if evidence else "no_evidence"})))
    if not evidence:
        return True

    result = {"status": "unavailable"}
    try:
        model = model_factory()
        request = {"observations": evidence, "alerts_available": report["alerts"] is not None,
                   "canary_available": report["canary_matches"] is not None}
        evaluation = Assessment.model_validate(model.choose([
            {"role": "system", "content": POLICY},
            {"role": "user", "content": json.dumps(request)},
        ]))
        if evaluation.assessment == "cross_protocol" and "chain:protocols" not in evidence:
            raise ValueError("Unverified cross-protocol assessment")
        for recommendation in evaluation.recommendations:
            if not set(recommendation.evidence) <= evidence.keys():
                raise ValueError("Unknown situation evidence")
            if recommendation.action == "review_block" and not any(
                key == "chain:protocols" or key.endswith((":rate_limited", ":session_ended"))
                for key in recommendation.evidence
            ):
                raise ValueError("Insufficient evidence for blocking review")
        result = dict(status="ready", model=model.name, **evaluation.model_dump())
    except (ValueError, RuntimeError, OSError, KeyError, IndexError, TypeError, httpx.HTTPError):
        pass  # The counts in the original report stay available.
    with closing(sqlite3.connect(destination, timeout=4)) as db, db:
        db.execute("UPDATE situation_analysis SET payload=? WHERE report_id=? AND started=? "
                   "AND json_extract(payload,'$.status')='pending'", (json.dumps(result), identifier, now))
    return True
