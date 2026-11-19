"""Operator review and control for synthetic knowledge document releases."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from business.publications import DocumentProposal, current_documents, list_proposals, request_transition, submit


def publication_router(require_operator):
    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/publications", response_class=HTMLResponse)
    def page():
        return Path(__file__).with_name("publications.html").read_text(encoding="utf-8")

    @router.get("/api/publication-documents")
    def documents():
        try:
            return current_documents()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.get("/api/publications")
    def index():
        try:
            return list_proposals()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.post("/api/publications", status_code=201)
    def create(proposal: DocumentProposal, request: Request):
        json_only(request)
        try:
            return submit(proposal)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/publications/{identifier}/{action}")
    def transition(identifier: str, action: Literal["approve", "rollback"], request: Request):
        json_only(request)
        try:
            return request_transition(identifier, action)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "Unknown proposal") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router


def json_only(request):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "需要 JSON 内容")
