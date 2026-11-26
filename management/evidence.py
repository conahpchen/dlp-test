"""Operator-only evidence that retains source and uncertainty."""

import json
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from audit.flow_evidence import matches
from management.layout import fragment, page, table_tools
from management.profiles import profile_page
from management.publications import json_only
from research.api import current_lab
from research.artifacts import graph_summary, scene_summary
from research.bindings import issue
from research.registration import evidence
from research import handoffs


class HandoffPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: list[Literal["web", "ssh", "mysql", "llm"]] = Field(min_length=1, max_length=5)


def evidence_router(require_operator):
    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/evidence", response_class=HTMLResponse)
    def sessions():
        return evidence_page("/evidence", "会话证据", "关联 Web 行为、AI 决策依据与 Agent 声明。", "session")

    @router.get("/chains", response_class=HTMLResponse)
    def chains():
        return evidence_page("/chains", "攻击链路", "从实际交付的 Canary 回溯来源，查看它在哪些入口出现。", "chain")

    @router.get("/profiles", response_class=HTMLResponse)
    def profiles(run: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$"),
                 session: str | None = Query(default=None, max_length=256)):
        lab = current_lab()
        try:
            return profile_page(lab, evidence_runs(lab), run, session,
                                lambda selected: observed_chains(lab, selected, include_network=False))
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc

    @router.get("/canary", response_class=HTMLResponse)
    def canary():
        return evidence_page("/canary", "Canary 凭据", "核对标记签发、真实响应回执和后续使用关联。", "canary")

    @router.get("/releases", response_class=HTMLResponse)
    def releases():
        return evidence_page("/releases", "场景发布", "审阅场景能力、发布准备和故障回退记录。", "release")

    def evidence_page(route, title, description, view):
        body, script = fragment("evidence")
        body = body.replace("<!-- timeline tools -->", table_tools("timeline-table", "搜索动作、路径或状态"))
        return page(route, title, description, body, script=script, view=view)

    @router.get("/api/evidence/runs")
    def runs():
        return evidence_runs(current_lab())

    @router.get("/api/evidence/runs/{run_id}")
    def detail(run_id: str):
        lab = current_lab()
        try:
            release = lab.release_status(run_id)
            with lab.connect() as db:
                release["handoff_route"] = handoffs.route_for(db, run_id)
            if version := (release["manifest"] or {}).get("graph_world_version"):
                release["graph"] = scene_summary(run_id, version)
            return {
                **evidence(lab, run_id),
                "capture": lab.capture_status(run_id),
                "canary_uses": lab.canary_uses(run_id),
                "credentials": issued_credentials(lab, run_id),
                "release": release,
            }
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc

    @router.put("/api/evidence/runs/{run_id}/handoff-policy")
    def change_handoff_policy(run_id: str, body: HandoffPolicy, request: Request):
        json_only(request)
        lab = current_lab()
        try:
            with lab.connect() as db:
                lab._plan(db, run_id)
                return {"route": handoffs.set_route(db, run_id, body.route)}
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc
        except ValueError as exc:
            raise HTTPException(422, "Unsupported Canary path") from exc

    @router.get("/api/evidence/runs/{run_id}/chains")
    def evidence_chains(run_id: str):
        try:
            return observed_chains(current_lab(), run_id)
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc

    @router.get("/api/evidence/generated-graphs/{version}")
    def generated_graph(version: int):
        graph = graph_summary(version)
        if graph is None:
            raise HTTPException(404, "Unknown generated graph")
        return graph

    @router.post("/api/evidence/runs/{run_id}/release/{action}")
    def change_release(run_id: str, action: Literal["activate", "rollback"], request: Request,
                       approve_isolated: bool = False):
        json_only(request)
        lab = current_lab()
        try:
            if action == "activate":
                return lab.activate(run_id, approve_isolated=approve_isolated)
            return lab.rollback(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post("/api/evidence/runs/{run_id}/bindings", status_code=201)
    def issue_binding(run_id: str, request: Request,
                      publication: str | None = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
                      target: Literal["web", "ssh", "mysql", "llm"] | None = None):
        json_only(request)
        try:
            return issue(current_lab(), run_id, publication, target=target)
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    return router


def evidence_runs(lab):
    with lab.connect(write=False) as db:
        plans = [json.loads(row[0]) for row in db.execute("SELECT plan FROM runs ORDER BY rowid DESC LIMIT 100")]
    active = lab.active()
    return [{**{field: plan.get(field) for field in ("run_id", "variant", "origin", "world_version")},
             "active": plan["run_id"] == active, "title": (plan.get("scene") or {}).get("title")}
            for plan in plans]


def issued_credentials(lab, run):
    with lab.connect() as db:
        rows = db.execute(
            "SELECT c.credential_id, c.source_session, c.lure_id, c.world_version, c.credential_type, "
            "c.expires_at, c.revoked_at, r.sent_at FROM canary_credentials c "
            "LEFT JOIN lure_response_receipts r ON c.credential_id=r.token_hash "
            "WHERE c.deployment_id=? ORDER BY c.expires_at DESC, c.credential_id LIMIT 100", (run,),
        )
        fields = ("credential_id", "source_session", "lure_id", "world_version", "credential_type",
                  "expires_at", "revoked_at", "sent_at")
        return [dict(zip(fields, row)) for row in rows]


def observed_chains(lab, run, *, include_network=True):
    """Only link matched use evidence to an actual response receipt."""
    with lab.connect() as db:
        lab._plan(db, run)
        rows = db.execute(
            "SELECT c.credential_id,c.lure_id,c.source_session,c.world_version,r.sent_at,"
            "u.id,u.entry,u.session,u.recorded_at,COALESCE((SELECT v.associated "
            "FROM canary_use_revisions v WHERE v.use_id=u.id ORDER BY v.revision DESC LIMIT 1),1), "
            "h.parent_id,h.parent_use,h.source_entry,h.target_entry "
            "FROM canary_credentials c JOIN lure_response_receipts r ON r.token_hash=c.credential_id "
            "JOIN lure_uses u ON u.token_hash=c.credential_id "
            "LEFT JOIN canary_handoffs h ON h.id=c.credential_id "
            "WHERE c.deployment_id=? AND (c.scope='source_web_session' OR h.id IS NOT NULL) ORDER BY u.id DESC LIMIT 100",
            (run,),
        ).fetchall()
    paths = {}
    for identifier, lure, session, version, sent, use, entry, target, observed, associated, parent, parent_use, source, scope in rows:
        path = paths.setdefault(identifier, {"credential_id": identifier, "source_entry": source or "web",
                                            "source_session": session, "resource": lure,
                                            "world_version": version, "sent_at": sent, "uses": [],
                                            "parent_credential_id": parent, "parent_use_id": parent_use,
                                            "target_entry": scope})
        path["uses"].append({"use_id": use, "entry": entry, "session": target,
                             "observed_at": observed, "associated": bool(associated)})
    if not include_network:
        return list(paths.values())
    sessions = {path["source_session"] for path in paths.values()}
    sessions.update(use["session"] for path in paths.values() for use in path["uses"])
    network = matches(os.environ.get("HONEYNET_FLOW_STATE_FILE"), sessions)
    for path in paths.values():
        path["network_evidence"] = network.get(path["source_session"], [])
        for use in path["uses"]:
            use["network_evidence"] = network.get(use["session"], [])
    return list(paths.values())
