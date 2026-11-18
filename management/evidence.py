"""Operator-only evidence that retains source and uncertainty."""

import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from management.publications import json_only
from research.api import current_lab
from research.bindings import issue
from research.registration import evidence


def evidence_router(require_operator):
    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/evidence", response_class=HTMLResponse)
    def page():
        return Path(__file__).with_name("evidence.html").read_text(encoding="utf-8")

    @router.get("/api/evidence/runs")
    def runs():
        lab = current_lab()
        with lab.connect() as db:
            plans = [json.loads(row[0]) for row in db.execute(
                "SELECT plan FROM runs ORDER BY rowid DESC LIMIT 100"
            )]
        return [{field: plan.get(field) for field in ("run_id", "variant", "origin", "world_version")}
                for plan in plans]

    @router.get("/api/evidence/runs/{run_id}")
    def detail(run_id: str):
        lab = current_lab()
        try:
            return {
                **evidence(lab, run_id),
                "canary_uses": lab.canary_uses(run_id),
                "release": lab.release_status(run_id),
            }
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc

    @router.post("/api/evidence/runs/{run_id}/release/{action}")
    def change_release(run_id: str, action: Literal["activate", "rollback"], request: Request):
        json_only(request)
        lab = current_lab()
        try:
            return (lab.activate if action == "activate" else lab.rollback)(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post("/api/evidence/runs/{run_id}/bindings", status_code=201)
    def issue_binding(run_id: str, request: Request):
        json_only(request)
        try:
            return issue(current_lab(), run_id)
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    return router
