"""Inspect public DWS world versions without reading secrets into the UI."""

from collections import Counter
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from business.atomic_state import read
from business.storage import location
from management.layout import esc, metric, page, table, table_tools

Kind = Literal["host", "identity", "object", "data", "relation"]
TYPES = {"host": "主机", "identity": "人员与身份", "object": "业务对象", "data": "文件与文档", "relation": "实体关系"}
CHANGES = {"added": "新增", "removed": "移除", "changed": "修订"}


def _view(state, version, selections):
    private = set(state.get("private_versions", ()))
    visible = {}
    names = {}
    for kind in TYPES:
        for entity_id, rows in state["facts"].get(kind, {}).items():
            fact = next((r for r in reversed(rows) if r["world_version"] <= version
                         and r["world_version"] not in private), None)
            if not fact or (kind == "data" and fact["payload"].get("deleted")):
                continue
            p = fact["payload"]
            label, detail = {
                "host": (p.get("hostname"), p.get("ip")),
                "identity": (p.get("username"), p.get("role")),
                "object": (p.get("object_key"), p.get("type")),
                "data": (p.get("path"), p.get("kind")),
                "relation": (p.get("kind"), None),
            }[kind]
            if kind != "relation":
                names[entity_id] = label
            visible[entity_id] = (kind, fact, label, detail)
    result = {}
    for entity_id, (kind, fact, label, detail) in visible.items():
        if kind not in selections:
            continue
        if kind == "relation":
            p = fact["payload"]
            origin = p["from_entity_id"]
            target = p["to_entity_id"]
            detail = f"{names.get(origin, origin)} → {names.get(target, target)}"
        result[entity_id] = {"entity": entity_id, "type": kind, "label": label, "detail": detail,
                             "revision": fact["version"], "world_version": fact["world_version"]}
    return result


def world_snapshot(version, baseline, kind, offset=0):
    checkpoint = location()
    if not checkpoint or not checkpoint.is_file():
        return {"current": None, "version": None, "versions": [], "baseline": None,
                "counts": {}, "total": 0, "entities": [], "diff_total": 0, "diff": []}
    state = read(checkpoint)
    current = state["meta"]["world_version"]
    private = set(state.get("private_versions", ()))
    fact_versions = {r["world_version"] for entries in state["facts"].values()
                     for rows in entries.values() for r in rows if r["world_version"] not in private}
    versions = sorted(fact_versions | ({current} - private), reverse=True)
    version = version if version is not None else versions[0]
    if version not in versions or (baseline is not None and (baseline not in versions or baseline >= version)):
        raise HTTPException(422, "请选择公开版本，且比较基线必须早于目标版本")
    all_rows = _view(state, version, TYPES)
    filtered = {key: row for key, row in all_rows.items() if kind is None or row["type"] == kind}
    entities = sorted(filtered.values(), key=lambda row: (row["type"], row["label"] or ""))
    changes = []
    if baseline is not None:
        previous = _view(state, baseline, [kind] if kind else TYPES)
        for entity in sorted(previous.keys() | filtered.keys()):
            before, after = previous.get(entity), filtered.get(entity)
            if before != after:
                changes.append({"entity": entity, "before": before, "after": after,
                                "change": "added" if not before else "removed" if not after else "changed"})
    return {"current": current, "version": version, "versions": versions, "baseline": baseline,
            "counts": dict(Counter(row["type"] for row in all_rows.values())),
            "total": len(entities), "entities": entities[offset:offset + 120],
            "diff_total": len(changes), "diff": changes[offset:offset + 120]}


def world_router(require_operator):
    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/api/world")
    def data(version: int | None = None, baseline: int | None = None, kind: Kind | None = None,
             offset: int = Query(0, ge=0)):
        return world_snapshot(version, baseline, kind, offset)

    @router.get("/world", response_class=HTMLResponse)
    def show(version: int | None = None, baseline: str | None = None, kind: Kind | None = None,
             offset: int = Query(0, ge=0)):
        if baseline and not (baseline.isascii() and baseline.isdecimal() and len(baseline) <= 16):
            raise HTTPException(422, "比较基线必须是版本号")
        baseline = int(baseline) if baseline else None
        error = ""
        try:
            snapshot = world_snapshot(version, baseline, kind, offset)
        except HTTPException as exc:
            snapshot = world_snapshot(None, None, kind)
            offset = 0
            error = f'<p class="note" role="alert">{esc(exc.detail)}</p>'
        current = snapshot["current"]
        version = snapshot["version"]
        baseline = snapshot["baseline"]
        versions = snapshot["versions"]
        metrics = metric("当前世界版本", current, "已提交的 DWS 快照", "mint")
        metrics += "".join(metric(label, snapshot["counts"].get(k, 0) if current is not None else None,
                                   "所选版本中的实体") for k, label in TYPES.items())
        choices = "".join(f'<option value="{n}" {"selected" if n == version else ""}>v{n}</option>'
                          for n in versions)
        baselines = "".join(f'<option value="{n}" {"selected" if n == baseline else ""}>v{n}</option>'
                            for n in versions)
        filters = []
        for key, label in ((None, "全部实体"), *TYPES.items()):
            params = {k: v for k, v in {"version": version, "baseline": baseline, "kind": key}.items()
                      if v is not None}
            active = ' aria-current="true"' if key == kind else ""
            filters.append(f'<a href="/world?{esc(urlencode(params))}"{active}>{label}</a>')
        controls = ""
        if versions:
            type_field = f'<input type="hidden" name="kind" value="{kind}">' if kind else ""
            controls = f'''<form class="actions" action="/world" method="get">{type_field}
            <label>目标版本 <select name="version">{choices}</select></label>
            <label>比较基线 <select name="baseline"><option value="">查看快照</option>{baselines}</select></label>
            <button type="submit">加载版本</button></form>'''
        rows = table(("类型", "名称", "摘要", "修订", "提交版本"), (
            (TYPES[r["type"]], r["label"], r["detail"] or "—", r["revision"], f'v{r["world_version"]}')
            for r in snapshot["entities"]))
        diff = ""
        if baseline is not None:
            diff_rows = []
            for change in snapshot["diff"]:
                before, after = change["before"], change["after"]
                item = after or before
                desc = lambda r: f'{r["detail"] or "—"} · 修订 {r["revision"]}' if r else "—"
                diff_rows.append((CHANGES[change["change"]], TYPES[item["type"]], item["label"],
                                  desc(before), desc(after)))
            diff = f'''<section class="panel"><div class="panel-header"><div><h2>版本差异</h2>
            <p>v{baseline} → v{version} · {snapshot["diff_total"]} 项变更</p></div></div>
            {table_tools("diff-rows")}<div id="diff-rows">{table(("变化", "类型", "实体", "基线", "目标"), diff_rows)}</div></section>'''
        pages = []
        total = max(snapshot["total"], snapshot["diff_total"])
        for label, step in (("前 120 条", max(0, offset - 120)), ("后 120 条", offset + 120)):
            if (label.startswith("前") and offset) or (label.startswith("后") and step < total):
                args = {k: v for k, v in {"version": version, "baseline": baseline, "kind": kind,
                                           "offset": step}.items() if v is not None}
                pages.append(f'<a class="button" href="/world?{esc(urlencode(args))}">{label}</a>')
        body = f'''<section class="metrics">{metrics}</section>{error}
        <section class="panel"><div class="panel-header"><div><h2>版本与实体</h2><p>当前筛选 {snapshot["total"]} 个实体</p></div>
        {controls}</div><div class="toolbar"><nav class="segments" aria-label="实体类型">{"".join(filters)}</nav>{"".join(pages)}</div>
        {table_tools("world-rows", "搜索实体、主机或类型")}<div id="world-rows">{rows}</div></section>{diff}'''
        return page("/world", "DWS 世界", "浏览公开版本中的合成实体和关系，并比较两个提交版本。", body,
                    actions='<a class="button" href="/publications">管理合成文档</a>')

    return router
