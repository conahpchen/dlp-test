"""Operator-only evidence that retains source and uncertainty."""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from research.api import current_lab
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
            }
        except KeyError as exc:
            raise HTTPException(404, "Unknown run") from exc

    return router
