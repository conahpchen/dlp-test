"""Optional self-reported classifications for a delivered research interaction."""

import json
import re
import secrets
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from research.analysis import analyze, session_agent_assessments


class AgentDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    framework: Literal["agent_sdk", "langgraph", "autogen", "custom", "unknown"]
    model_family: Literal["openai", "anthropic", "gemini", "open_weight", "other", "unknown"]
    instance_kind: Literal["single", "multi_agent", "unknown"]
    task: Literal["lookup", "verification", "evaluation", "other", "unknown"]


async def declaration(request):
    if request.headers.get("content-type", "").partition(";")[0] != "application/json":
        raise HTTPException(415, "需要 JSON 内容")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 512:
            raise HTTPException(413, "声明过长")
        body.extend(chunk)
    try:
        return AgentDeclaration.model_validate_json(body).model_dump()
    except ValidationError:
        # Invalid input can contain a secret; do not reflect the payload.
        raise HTTPException(422, "请选择允许的声明项") from None


def check_session(db, run, session):
    return db.execute(
        "SELECT 1 FROM canary_credentials c JOIN lure_response_receipts r "
        "ON r.token_hash=c.credential_id WHERE c.deployment_id=? AND c.source_session=? "
        "AND c.expires_at>unixepoch() AND c.revoked_at IS NULL LIMIT 1", (run, session)
    ).fetchone()


def register(lab, run, session, claim):
    with lab.connect() as db:
        lab._plan(db, run)
        if not check_session(db, run, session):
            raise PermissionError("Registration requires a delivered run")
        existing = db.execute(
            "SELECT registration_id,claim FROM agent_registrations WHERE run=? AND session=?",
            (run, session),
        ).fetchone()
        if existing:
            if json.loads(existing[1]) != claim:
                raise ValueError("声明已登记")
            return existing[0]
        identifier = secrets.token_hex(16)
        db.execute("INSERT INTO agent_registrations(run,session,registration_id,claim) VALUES (?,?,?,?)",
                   (run, session, identifier, json.dumps(claim)))
        return identifier


def describe_registration(lab, run, session):
    with lab.connect() as db:
        lab._plan(db, run)
        if not check_session(db, run, session):
            raise PermissionError("Registration requires a delivered run")
    return {
        "optional": True,
        "fields": {name: spec["enum"] for name, spec in AgentDeclaration.model_json_schema()["properties"].items()},
    }


def install_registration(app, lab_provider):
    @app.api_route("/decoy/{run_id}/register", methods=["GET", "POST"])
    async def register_route(run_id: str, request: Request):
        session = request.cookies.get("research_session", "")
        if not re.fullmatch("[a-f0-9]{32}", session):
            raise HTTPException(404, "接口不存在")
        lab = lab_provider()
        try:
            if request.method == "GET":
                return await run_in_threadpool(describe_registration, lab, run_id, session)
            claim = await declaration(request)
            identifier = await run_in_threadpool(register, lab, run_id, session, claim)
            return {"registered": True, "registration_id": identifier}
        except (KeyError, PermissionError):
            raise HTTPException(404, "接口不存在") from None
        except ValueError:
            raise HTTPException(409, "声明已登记") from None


def evidence(lab, run, *, include_registrations=True, include_agent_assessments=False):
    with lab.connect() as db:
        plan = lab._plan(db, run)
        rows = db.execute(
            "SELECT session,registration_id,claim,recorded_at FROM agent_registrations "
            "WHERE run=? ORDER BY rowid", (run,),
        ).fetchall() if include_registrations else []
        events = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE run=? ORDER BY id", (run,),
        )]
        decisions = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM decisions WHERE run=? ORDER BY id", (run,),
        )]
    profiles = {row["session"]: row for row in analyze(events)} if rows else {}
    evidence_rows = []
    for session, registration_id, claim, recorded_at in rows:
        profile = profiles.get(session, {})
        evidence_rows.append({
            "self_reported": json.loads(claim),
            "server_observed": {
                "registration_id": registration_id,
                "session": session,
                "received_at": recorded_at,
                "world_version": plan["world_version"],
                "recorded_event_count": profile.get("actions", 0),
            },
            "inferred": {
                "hypothesis": profile.get("hypothesis", "unknown"),
                "confidence": profile.get("confidence", "undetermined"),
            },
        })
    initial_titles = {f"documents/{doc['id']}": doc["path"] for doc in plan["documents"]}
    initial_titles.update({f"scene/{node['id']}": node["title"]
                           for node in (plan.get("scene") or {}).get("nodes", [])})
    graphs = [plan.get("scene") or {}]
    graphs.extend((record.get("decision") or {}).get("scene_patch") or {} for record in decisions)
    known_resources = {"index", "interact", *initial_titles}
    for graph in graphs:
        known_resources.update(f"scene/{node['id']}" for node in graph.get("nodes", []))
        known_resources.update(f"service/{service['path']}" for service in graph.get("services", []))
    timeline = []
    for event in events:
        what = event["what"]
        resource = what.get("resource")
        if what["kind"] == "research.input":
            observed = what.get("observed_scene")
            if isinstance(observed, dict):
                resource = observed.get("resource") or (
                    resource if resource in {"index", "interact", *initial_titles} else "未识别的场景路径"
                )
            elif resource not in known_resources:
                resource = "未识别的场景路径"
        title = what.get("title")
        if not title and what["kind"] == "research.delivery" and not what.get("policy_revision"):
            title = initial_titles.get(resource)
        timeline.append({
            "event_id": event["event_id"],
            "entry": event["entry"],
            "session": event["session"],
            "kind": what["kind"],
            "world_version": event["dws_version"],
            "logical_time": event["ts_hlc"],
            "resource": resource,
            "source_network": (event.get("src") or {}).get("ip"),
            "method": what.get("method"),
            "status": what.get("status"),
            "title": title,
        })
    generation = []
    for record in decisions:
        decision = record.get("decision") or {}
        patch = decision.get("scene_patch") or {}
        generation.append({
            "session": record["session"],
            "status": record["status"],
            "business_activity": record.get("business_activity"),
            "intent": decision.get("intent"),
            "nodes": [{"id": node["id"], "title": node["title"]} for node in patch.get("nodes", [])],
            "services": [{"method": service["method"], "path": service["path"]}
                         for service in patch.get("services", [])],
            "evidence_event_ids": decision.get("evidence_event_ids") or record.get("evidence_window", []),
            "error_code": record.get("error_code"),
        })
    data = {"registrations": evidence_rows, "timeline": timeline, "generation": generation}
    if include_agent_assessments:
        data["agent_assessments"] = session_agent_assessments(events, decisions)
    return data
