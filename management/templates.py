"""Authenticated editing of immutable research template revisions."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from management.layout import fragment, page
from management.publications import json_only

from research import template_scenes
from research.api import current_lab


class TemplateEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    variant: Literal["plain", "agent_note"]
    goal: str = Field(min_length=1, max_length=2000)
    template_id: str | None = Field(default=None, pattern=r"^(operations|[a-f0-9]{32})$")
    expected_revision: int = Field(default=0, ge=0)


class SceneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(pattern=r"^(operations|[a-f0-9]{32})$")
    revision: int = Field(ge=1, le=9223372036854775807)


def template_router(require_operator):
    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/templates", response_class=HTMLResponse)
    def show():
        body, script = fragment("templates")
        return page("/templates", "场景模板", "建立可复用的生成目标，并管理模板的追加修订。", body, script=script)

    @router.get("/api/templates")
    def index():
        return current_lab().templates()

    @router.post("/api/templates")
    def save(edit: TemplateEdit, request: Request):
        json_only(request)
        if (edit.template_id is None) != (edit.expected_revision == 0):
            raise HTTPException(422, "编辑已有模板时需要模板 ID 和当前修订号")
        try:
            return current_lab().save_template(**edit.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/api/templates/{template_id}/revisions/{revision}/scene")
    def scene_status(template_id: str = Path(pattern=r"^(operations|[a-f0-9]{32})$"),
                     revision: int = Path(ge=1, le=9223372036854775807)):
        return template_scenes.status(current_lab(), template_id, revision)

    @router.post("/api/template-scenes", status_code=202)
    def generate(body: SceneRequest, request: Request):
        json_only(request)
        try:
            return template_scenes.enqueue(current_lab(), body.template_id, body.revision)
        except KeyError as exc:
            raise HTTPException(404, "找不到指定的模板修订") from exc
        except OverflowError as exc:
            raise HTTPException(429, "生成队列已满，请稍后重试") from exc

    return router
