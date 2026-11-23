"""Authenticated timeline of passive situation reports."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from audit.situations import list_reports
from management.alerts import KINDS, SOURCES
from management.layout import esc, page, table

TITLES = {"hourly": "小时态势快照", "cross_protocol": "同一 Canary 链在多个入口命中"}
ASSESSMENTS = {"quiet": "活动较少", "probing": "疑似探测", "credential_activity": "凭据相关活动",
               "cross_protocol": "跨协议 Canary 使用", "inconclusive": "证据不足"}
SEVERITIES = {"low": "低", "medium": "中", "high": "高"}
ACTIONS = {"observe": "继续观察", "notify_operator": "通知管理员", "review_block": "提交阻断复核"}
ANALYSIS_STATES = {"pending": "模型分析中", "no_evidence": "当前窗口暂无可供研判的信号",
                   "unavailable": "模型研判暂不可用，请查看原始观测"}


def evidence_label(key):
    if key == "chain:protocols":
        return "同一 Canary 链覆盖多个协议"
    signal, entry, *kind = key.split(":")
    label = "Canary 命中" if signal == "canary" else KINDS[kind[0]]
    return f"{SOURCES[entry]} {label}"


def model_panel(analysis):
    if analysis is None:
        return ""
    if analysis["status"] != "ready":
        return f'<p class="status">{ANALYSIS_STATES[analysis["status"]]}</p>'
    heading = ASSESSMENTS[analysis["assessment"]] + " · " + SEVERITIES[analysis["severity"]] + "风险"
    rows = table(("建议", "依据"), (
        (ACTIONS[item["action"]], "、".join(evidence_label(key) for key in item["evidence"]))
        for item in analysis["recommendations"]), "暂无建议")
    return (f'<h3>模型研判 · {esc(analysis["model"])}</h3><p>{esc(heading)}</p>{rows}'
            '<p>建议供管理员复核。报告不会执行阻断。</p>')


def situations_router(require_operator):
    router = APIRouter(dependencies=[Depends(require_operator)])

    @router.get("/api/situations")
    def data():
        return list_reports()

    @router.get("/situations", response_class=HTMLResponse)
    def show():
        cards = []
        for row in list_reports():
            keys = row["canary_matches"]
            alerts = row["alerts"]
            alert_table = table(("入口", "观测", "条数"), (
                (SOURCES.get(item["entry"], item["entry"]), KINDS.get(item["kind"], item["kind"]), item["count"])
                for item in alerts or []), "采集源尚未就绪" if alerts is None else "窗口内暂无观测")
            canary_table = table(("命中入口", "有效关联数"),
                                 ((SOURCES.get(k, k), v) for k, v in (keys or {}).items()),
                                 "凭据采集源尚未就绪" if keys is None else "窗口内暂无有效关联")
            link = ""
            if row.get("run"):
                url = esc(urlencode({"run": row["run"]}))
                link = f'<a href="/chains?{url}">查看场景的链路证据 →</a>'
            title = TITLES[row["kind"]]
            cards.append(f'''<section class="panel"><div class="panel-header"><div><h2>{title}</h2>
            <p>{esc(row["window_start"])} — {esc(row["generated_at"])} UTC</p></div>{link}</div>
            <div class="split">{alert_table}{canary_table}</div>{model_panel(row.get("analysis"))}</section>''')
        body = "".join(cards) or '<p class="empty">尚未收到态势报告。启动控制面报告任务后会在此展示。</p>'
        return page("/situations", "态势报告", "查看入口观测、跨协议 Canary 和模型处置建议。",
                    body, actions='<a class="button" href="/situations">刷新报告</a>')

    return router
