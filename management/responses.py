"""Two-role approval console for synthetic credentials and evidence associations."""

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from management.layout import fragment, page
from management.publications import json_only
from research.api import current_lab
from research.responses import decide, list_requests, submit


class ResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    run: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: Literal["revoke", "detach", "restore"]
    target: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=300)


class ResponseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=3, max_length=300)


def responses_router(require_response_actor):
    router = APIRouter()

    @router.get("/responses", response_class=HTMLResponse, dependencies=[Depends(require_response_actor)])
    def show():
        body, script = fragment("responses")
        return page("/responses", "证据处置", "提交、审核和追踪 Canary 撤销及关联修订。", body, script=script)

    @router.get("/api/responses")
    def index(actor: str = Depends(require_response_actor)):
        return {"actor": actor, "requests": list_requests(current_lab())}

    @router.post("/api/responses", status_code=201)
    def propose(body: ResponseRequest, request: Request, actor: str = Depends(require_response_actor)):
        json_only(request)
        if actor != "operator":
            raise HTTPException(403, "请使用运营账号提交")
        pattern = r"[0-9a-f]{64}" if body.kind == "revoke" else r"[1-9][0-9]{0,17}"
        if not re.fullmatch(pattern, body.target):
            raise HTTPException(422, "请检查凭据标识或证据编号")
        try:
            return submit(current_lab(), **body.model_dump())
        except KeyError as exc:
            raise HTTPException(404, "此场景查不到该对象") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/responses/{identifier}/decision")
    def review(body: ResponseDecision, request: Request, identifier: int = Path(gt=0, le=9223372036854775807),
               actor: str = Depends(require_response_actor)):
        json_only(request)
        if actor != "reviewer":
            raise HTTPException(403, "请使用独立审核账号")
        try:
            return decide(current_lab(), identifier, body.decision == "approve", body.reason)
        except KeyError as exc:
            raise HTTPException(404, "找不到处置请求") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
