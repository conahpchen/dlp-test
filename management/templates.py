"""Authenticated editing of immutable research template revisions."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from research.api import current_lab


class TemplateEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    variant: Literal["plain", "agent_note"]
    goal: str = Field(min_length=1, max_length=2000)
    template_id: str | None = Field(default=None, pattern=r"^(operations|[a-f0-9]{32})$")
    expected_revision: int = Field(default=0, ge=0)


def template_router(require_operator):
    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/templates", response_class=HTMLResponse)
    def page():
        return (Path(__file__).parent / "templates.html").read_text(encoding="utf-8")

    @router.get("/api/templates")
    def index():
        return current_lab().templates()

    @router.post("/api/templates")
    def save(edit: TemplateEdit, request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "需要 JSON 内容")
        if (edit.template_id is None) != (edit.expected_revision == 0):
            raise HTTPException(422, "编辑已有模板时需要模板 ID 和当前修订号")
        try:
            return current_lab().save_template(**edit.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
