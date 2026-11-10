"""Seven-step Dev→Test→Ops workflow, through the same APIs as browser users."""

import json
import os
from agent_runtime.adapter import ToolAdapter
from contracts.agent_personas import load_personas


def run_demo_workflow(base_url: str, client=None) -> dict:
    adapters = {}
    for p in load_personas():
        a = ToolAdapter(
            base_url=base_url,
            username=p["username"],
            password=p["username"] + "-password",
            allowlist=p["tools"],
            agent_id=p["agent_id"],
            workstation_ip=p["workstation_ip"],
            client=client,
        )
        if not a.login():
            raise RuntimeError(f"Login failed for {p['username']}")
        adapters[p["role"]] = a
    steps = []

    def call(role, tool, **kwargs):
        result = adapters[role].call(tool, **kwargs)
        if result["status"] != 200:
            raise RuntimeError(f"{role}/{tool}: {result['status']} {result['body']}")
        return result["body"]

    ticket = call(
        "biz",
        "ticket_create",
        body={
            "title": "工单列表高峰期超时排查",
            "description": "09:30 列表查询延迟升高，要求核对索引与回归测试。",
            "application": "APP-COLLAB",
        },
    )
    key = ticket["key"]
    steps.append(dict(step=1, who="biz", action="ticket_create", key=key))
    call(
        "dev",
        "ticket_status",
        key=key,
        correlation_id=key,
        body={"status": "in_progress"},
    )
    change = call(
        "dev",
        "change_create",
        correlation_id=key,
        body={
            "ticket": key,
            "title": "查询索引优化",
            "plan": "在测试环境核对组合索引与查询计划，再提交生产窗口。",
            "rollback": "删除新增索引，恢复上一版本查询路径。",
        },
    )
    ck = change["key"]
    steps.append(dict(step=2, who="dev", action="change_create", key=ck))
    call(
        "test",
        "change_test",
        key=ck,
        correlation_id=key,
        body={
            "passed": True,
            "note": "列表、分页和状态筛选回归通过；执行计划使用组合索引。",
        },
    )
    steps.append(dict(step=3, who="test", action="change_test"))
    call(
        "manager",
        "change_approve",
        key=ck,
        correlation_id=key,
        body={"note": "测试与回滚方案核对完成，批准执行。"},
    )
    steps.append(dict(step=4, who="manager", action="change_approve"))
    call(
        "ops",
        "change_deploy",
        key=ck,
        correlation_id=key,
        body={"note": "发布记录已归档，服务健康检查通过。"},
    )
    steps.append(dict(step=5, who="ops", action="change_deploy"))
    databases = call("dba", "database_view", correlation_id=key)
    call(
        "dba",
        "ticket_comment",
        key=key,
        correlation_id=key,
        body={
            "text": f"已检查 {len(databases)} 个数据库实例的备份记录及慢查询摘要，关联变更 {ck}。"
        },
    )
    steps.append(dict(step=6, who="dba", action="database_view"))
    call(
        "test",
        "ticket_status",
        key=key,
        correlation_id=key,
        body={"status": "resolved"},
    )
    call(
        "manager",
        "ticket_status",
        key=key,
        correlation_id=key,
        body={"status": "closed"},
    )
    steps.append(dict(step=7, who="manager", action="ticket_close"))
    detail = call("dev", "ticket_read", key=key, correlation_id=key)
    reporter = client or adapters["manager"].client
    for a in adapters.values():
        for ev in a.events:
            r = reporter.post(
                "/api/events/ingest",
                content=json.dumps(ev, ensure_ascii=False),
                headers=(
                    {"x-control-token": os.environ["HONEYNET_CONTROL_TOKEN"]}
                    if os.environ.get("HONEYNET_CONTROL_TOKEN")
                    else {}
                ),
            )
            r.raise_for_status()
    return dict(
        steps=steps,
        ticket=detail,
        change=ck,
        correlation_ok=detail["status"] == "closed",
        agent_events_ingested=sum(len(a.events) for a in adapters.values()),
    )
