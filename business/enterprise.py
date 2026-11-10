"""DWS-backed enterprise records and release workflow."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fastapi import Request, HTTPException
from dws.ids import entity_uuid

PEOPLE = {
    "dev01": ("林砚", "研发部"),
    "ops01": ("周岑", "平台运维组"),
    "test01": ("陈曦", "质量保障组"),
    "dba01": ("许宁", "数据平台组"),
    "biz01": ("苏禾", "业务运营组"),
    "manager01": ("沈知行", "研发管理部"),
}
FLOW = {
    "open": ["in_progress", "closed"],
    "in_progress": ["resolved", "closed"],
    "resolved": ["closed", "in_progress"],
    "closed": [],
}


def stamp(world):
    return datetime.fromtimestamp(
        int(world.meta["now_hlc"].split(".")[0]), timezone.utc
    ).isoformat(timespec="seconds")


def enrich_seed(seed):
    seed = deepcopy(seed)
    facts = seed["initial_facts"]
    objects = facts.setdefault("object", [])
    if any(o["type"] == "application" for o in objects):
        return seed
    anchor = datetime.fromtimestamp(seed["meta"]["base_seconds"], timezone.utc)

    def date(days, minutes=0):
        return (anchor - timedelta(days=days) + timedelta(minutes=minutes)).isoformat(
            timespec="seconds"
        )

    def obj(t, k, d):
        objects.append(dict(type=t, object_key=k, data=d))

    for ident in facts["identity"]:
        if ident["username"] in PEOPLE:
            ident.update(
                display_name=PEOPLE[ident["username"]][0],
                department=PEOPLE[ident["username"]][1],
            )
    for key, name, host, owner, version in [
        ("APP-COLLAB", "研发服务平台", "oa01", "dev01", "2.8.3"),
        ("APP-DATA", "业务数据服务", "db01", "dba01", "1.14.2"),
        ("APP-AI", "知识检索助手", "ai01", "dev01", "0.9.6"),
    ]:
        obj(
            "application",
            key,
            dict(
                name=name,
                hosts=[host],
                owner=owner,
                release_version=version,
                environment="production",
                tier="T1",
                dependencies=["APP-DATA"] if key != "APP-DATA" else [],
            ),
        )
    scenarios = [
        (
            "工单列表高峰期查询超时",
            "APP-COLLAB",
            "dev01",
            "in_progress",
            "近三日 09:30–10:00，列表 p95 从 420ms 升至 2.8s，影响约 18% 查询。COUNT 查询缺少组合索引，关联 CHG-0821。",
        ),
        (
            "日报归档与备份窗口冲突",
            "APP-DATA",
            "dba01",
            "open",
            "02:00 全量备份与归档重叠 17 分钟，拟将归档后移至 03:10；需验证复制延迟低于 5 秒。",
        ),
        (
            "知识助手接入说明补充",
            "APP-AI",
            "dev01",
            "open",
            "补充请求限额、错误码及会话超时说明，接口负责人确认后发布。",
        ),
        (
            "附件列表切换分页偶发空白",
            "APP-COLLAB",
            "test01",
            "resolved",
            "旧请求覆盖新响应，已增加请求序列校验。测试环境连续切换分页 50 次未复现，待申请人确认。",
        ),
        (
            "数据库连接池水位复核",
            "APP-DATA",
            "dba01",
            "closed",
            "max_connections=300，应用连接池上限 80。对比一周峰值后确认保留运维连接配额。",
        ),
        (
            "季度审批权限复核",
            "APP-COLLAB",
            "manager01",
            "in_progress",
            "逐部门核对审批人，关闭过期临时授权；研发与数据平台已完成，待运营确认。",
        ),
        (
            "报表导出字段顺序调整",
            "APP-DATA",
            "dev01",
            "closed",
            "部门编码移至部门名称之后，保留旧版表头兼容，运营已完成验收。",
        ),
        (
            "知识索引滚动升级",
            "APP-AI",
            "ops01",
            "closed",
            "滚动切换检索索引，保留上一版用于回滚，观察 30 分钟无异常后结束维护。",
        ),
    ]
    for i, (title, app, owner, status, desc) in enumerate(scenarios, 1):
        created = date(12 - i)
        updated = date(12 - i, 120)
        history = [dict(step="提交", actor="biz01", action="创建工单", time=created)]
        if status != "open":
            history.append(
                dict(step="受理", actor=owner, action="开始处理", time=date(12 - i, 20))
            )
        if status in ("resolved", "closed"):
            history.append(
                dict(
                    step="验证",
                    actor="test01",
                    action="回归通过",
                    time=date(12 - i, 95),
                )
            )
        if status == "closed":
            history.append(
                dict(step="确认", actor="biz01", action="关闭工单", time=updated)
            )
        obj(
            "ticket",
            f"INC-{2400+i}",
            dict(
                title=title,
                description=desc,
                application=app,
                type="故障" if i in (1, 4) else "需求",
                priority="P1-高" if i == 1 else "P2-中",
                status=status,
                reporter="biz01",
                assignee=owner,
                department=PEOPLE[owner][1],
                created_at=created,
                updated_at=updated,
                sla_due=date(11 - i),
                comments=[dict(user=owner, text=desc, time=updated)],
                approval_flow=history,
                attachments=[],
            ),
        )
    # Recurring operational work provides a dated history rather than random filler.
    for week in range(1, 13):
        for idx, (application, owner, task, evidence) in enumerate(
            [
                (
                    "APP-COLLAB",
                    "ops01",
                    "服务巡检",
                    "检查工单列表、附件读取和审批入口；核对值班告警，无新增未处理项。",
                ),
                (
                    "APP-DATA",
                    "dba01",
                    "备份抽检",
                    "检查全量备份与 binlog 链完整性，抽检恢复后的工单主记录和评论关联。",
                ),
                (
                    "APP-AI",
                    "dev01",
                    "接口兼容性复核",
                    "验证模型目录、会话超时和错误码约定；更新接入方兼容性检查记录。",
                ),
            ]
        ):
            day = week * 7 + idx
            key = f"OPS-{week:02d}{idx+1:02d}"
            obj(
                "ticket",
                key,
                dict(
                    title=f"{application} 第 {week} 期{task}",
                    description=evidence,
                    application=application,
                    type="咨询",
                    priority="P3-低",
                    status="closed",
                    reporter=owner,
                    assignee=owner,
                    department=PEOPLE[owner][1],
                    created_at=date(day, 10),
                    updated_at=date(day, 85),
                    comments=[dict(user=owner, text=evidence, time=date(day, 70))],
                    approval_flow=[
                        dict(
                            step="计划任务",
                            actor=owner,
                            action="开始检查",
                            time=date(day, 10),
                        ),
                        dict(
                            step="检查记录",
                            actor=owner,
                            action="完成",
                            time=date(day, 70),
                        ),
                        dict(
                            step="归档",
                            actor="manager01",
                            action="确认归档",
                            time=date(day, 85),
                        ),
                    ],
                    attachments=[],
                ),
            )
    obj(
        "change_request",
        "CHG-0821",
        dict(
            title="工单列表查询索引优化",
            ticket="INC-2401",
            application="APP-COLLAB",
            owner="dev01",
            status="draft",
            risk="medium",
            plan="先在测试环境增加 status、created_at 组合索引，核对执行计划及写入延迟。",
            rollback="删除新增索引，恢复 2.8.3 查询路径。",
            test_result=None,
            approver=None,
            deployment=None,
            history=[],
        ),
    )
    obj(
        "database",
        "DB-CORE",
        dict(
            name="collab_core",
            host="db01",
            owner="dba01",
            application="APP-DATA",
            engine="MySQL 8.0.35",
            backup_policy="每日 02:00 全量，binlog 保留 7 天",
            last_backup=date(1, 120),
            backup_status="success",
            retention_days=14,
            tables=[
                dict(name="service_ticket", rows=18426, description="工单主记录"),
                dict(name="ticket_comment", rows=62918, description="处理记录"),
                dict(name="release_record", rows=384, description="发布及回滚记录"),
            ],
            slow_queries=[
                dict(
                    digest="SELECT id, title FROM service_ticket WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    p95_ms=2810,
                    calls=1264,
                    ticket="INC-2401",
                )
            ],
        ),
    )
    for i, (title, body, owner) in enumerate(
        [
            (
                "发布与回滚规范",
                "生产发布必须关联工单、通过测试并由负责人审批。部署前确认备份可用，失败时记录回滚原因并保留发布日志。",
                "ops01",
            ),
            (
                "数据库备份检查清单",
                "检查 DB-CORE 备份状态、保留周期。在隔离测试环境验证恢复，每周抽检表行数和关键查询。",
                "dba01",
            ),
            (
                "高峰期查询排查记录",
                "关联 INC-2401 / CHG-0821。对比执行计划与慢查询摘要，索引变更须先完成回归，避免在峰值执行全表扫描。",
                "dev01",
            ),
        ],
        1,
    ):
        obj(
            "document",
            f"KB-{i:04d}",
            dict(
                title=title,
                body=body,
                author=owner,
                updated_at=date(3),
                space="研发运维",
                revision=i + 1,
            ),
        )
    obj(
        "announcement",
        "AN-0100",
        dict(
            title="本周生产变更窗口安排",
            body="周四 20:00–21:00 为统一变更窗口，请附测试报告及回滚方案。紧急故障按值班升级流程处理。",
            author="manager01",
            published_at=date(2),
        ),
    )
    return seed


def register(app):
    from business import app as b

    async def payload(request):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(422, "请求必须为 JSON 对象")
        if not isinstance(body, dict):
            raise HTTPException(422, "请求必须为 JSON 对象")
        return body

    def session(r):
        try:
            return b._require(r)
        except PermissionError:
            raise HTTPException(401, "未认证")

    def records(kind):
        with b._lock:
            return [
                dict(
                    key=f.payload["object_key"],
                    ticket_id=f.payload["object_key"],
                    version=f.version,
                    **deepcopy(f.payload["data"]),
                )
                for f in b.get_engine().world.all_current_cls("object")
                if f.payload["type"] == kind
            ]

    def find(kind, key):
        f = b.get_engine().world.current(
            "object", entity_uuid("object", f"{kind}:{key}")
        )
        if not f:
            raise HTTPException(404, "记录不存在")
        return f

    def text(body, key, limit=6000):
        v = body.get(key)
        if not isinstance(v, str) or not v.strip() or len(v) > limit:
            raise HTTPException(422, f"{key} 必填，最长 {limit} 字符")
        return v.strip()

    def users():
        return {
            f.payload["username"]: f.payload
            for f in b.get_engine().world.all_current_cls("identity")
            if f.payload["username"] in PEOPLE
        }

    def actor(s, allowed):
        if s["username"] not in allowed:
            raise HTTPException(403, "当前岗位无权执行此操作")

    def save(kind, key, data, s, path, version=None):
        from dws.world import CommitRejected
        import json
        from jsonschema import validate, ValidationError

        schema_path = b.REPO / "contracts" / "object_types" / f"{kind}.schema.json"
        try:
            validate(data, json.loads(schema_path.read_text()))
        except ValidationError as ex:
            raise HTTPException(422, ex.message)

        e = b.get_engine()
        old = e.world.current("object", entity_uuid("object", f"{kind}:{key}"))
        if version is not None and (not old or old.version != version):
            raise HTTPException(409, "记录已更新，请刷新")
        data = deepcopy(data)
        data["updated_at"] = stamp(e.world)
        try:
            e.commit_external(
                None,
                [("object", dict(type=kind, object_key=key, data=data))],
                correlation_id=data.get("ticket", key),
                actor_id=s["username"],
            )
        except CommitRejected as ex:
            raise HTTPException(409, str(ex))
        b._audit(s["username"], "POST", path, 200)
        b._emit(
            b._biz_event(
                e, s["username"], s["cred_hash"], path, data.get("ticket", key)
            )
        )
        from business.storage import persist

        persist(e, b._audit_rows, b._events)
        return dict(key=key, ticket_id=key, **data)

    def listing(r, kind):
        s = session(r)
        b._audit(s["username"], "GET", r.url.path, 200)
        q = r.query_params.get("q", "").casefold()
        return [
            t
            for t in records(kind)
            if (not q or q in str(t).casefold())
            and all(
                not r.query_params.get(k) or t.get(k) == r.query_params[k]
                for k in ("status", "assignee", "application")
            )
        ]

    @app.get("/api/people")
    def people(request: Request):
        session(request)
        return [
            dict(
                username=u,
                display_name=p.get("display_name", u),
                department=p.get("department", ""),
            )
            for u, p in users().items()
        ]

    @app.get("/api/tickets")
    @app.get("/api/tickets/all")
    def tickets(request: Request):
        return listing(request, "ticket")

    @app.post("/api/tickets")
    @app.post("/api/tickets/create")
    async def create(request: Request):
        s = session(request)
        body = await payload(request)
        with b._lock:
            title = text(body, "title", 200)
            owner = body.get("assignee")
            if owner and owner not in users():
                raise HTTPException(422, "处理人不存在")
            application = body.get("application", "APP-COLLAB")
            find("application", application)
            typ = body.get("type", "故障")
            priority = body.get("priority", "P2-中")
            if typ not in ("故障", "需求", "变更", "咨询") or priority not in (
                "P0-紧急",
                "P1-高",
                "P2-中",
                "P3-低",
            ):
                raise HTTPException(422, "类型或优先级无效")
            n = 1
            while any(t["key"] == f"HD-{n:04d}" for t in records("ticket")):
                n += 1
            key = f"HD-{n:04d}"
            now = stamp(b.get_engine().world)
            return save(
                "ticket",
                key,
                dict(
                    title=title,
                    description=body.get("description", ""),
                    status="open",
                    reporter=s["username"],
                    assignee=owner,
                    application=application,
                    type=typ,
                    priority=priority,
                    department=body.get(
                        "department", PEOPLE.get(s["username"], ("", "研发部"))[1]
                    ),
                    created_at=now,
                    comments=[],
                    approval_flow=[
                        dict(
                            step="提交",
                            actor=s["username"],
                            action="创建工单",
                            time=now,
                        )
                    ],
                    attachments=[],
                ),
                s,
                request.url.path,
            )

    @app.get("/api/tickets/{key}")
    def detail(key: str, request: Request):
        session(request)
        f = find("ticket", key)
        return dict(
            key=key, ticket_id=key, version=f.version, **deepcopy(f.payload["data"])
        )

    @app.post("/api/tickets/{key}/status")
    @app.post("/api/tickets/{key}/approve")
    async def status(key: str, request: Request):
        s = session(request)
        body = await payload(request)
        with b._lock:
            data = deepcopy(find("ticket", key).payload["data"])
            if request.url.path.endswith("/approve"):
                actor(s, {"manager01"})
            target = body.get("status", body.get("new_status", "in_progress"))
            if target not in FLOW.get(data["status"], []):
                raise HTTPException(400, "非法状态迁移")
            data["status"] = target
            data.setdefault("approval_flow", []).append(
                dict(
                    step="状态变更",
                    actor=s["username"],
                    action=target,
                    time=stamp(b.get_engine().world),
                    comment=body.get("comment", ""),
                )
            )
            return save("ticket", key, data, s, request.url.path, body.get("version"))

    @app.post("/api/tickets/{key}/comments")
    async def comment(key: str, request: Request):
        s = session(request)
        body = await payload(request)
        value = text(body, "text")
        with b._lock:
            data = deepcopy(find("ticket", key).payload["data"])
            data.setdefault("comments", []).append(
                dict(user=s["username"], text=value, time=stamp(b.get_engine().world))
            )
            return save("ticket", key, data, s, request.url.path, body.get("version"))

    @app.post("/api/tickets/{key}/assign")
    async def assign(key: str, request: Request):
        s = session(request)
        body = await payload(request)
        owner = text(body, "assignee", 80)
        if owner not in users():
            raise HTTPException(422, "处理人不存在")
        with b._lock:
            data = deepcopy(find("ticket", key).payload["data"])
            data["assignee"] = owner
            data.setdefault("approval_flow", []).append(
                dict(
                    step="分派",
                    actor=s["username"],
                    action=owner,
                    time=stamp(b.get_engine().world),
                )
            )
            return save("ticket", key, data, s, request.url.path, body.get("version"))

    @app.get("/api/dashboard")
    def dashboard(request: Request):
        session(request)
        rows = records("ticket")
        return dict(
            total=len(rows),
            **{k: sum(t["status"] == k for t in rows) for k in FLOW},
            p0=sum(
                t["priority"].startswith("P0")
                and t["status"] not in ("resolved", "closed")
                for t in rows
            ),
            nodes_online=len(b.get_engine().world.all_current_cls("host")),
        )

    @app.get("/api/applications")
    def applications(request: Request):
        return listing(request, "application")

    @app.get("/api/databases")
    def databases(request: Request):
        return listing(request, "database")

    @app.get("/api/documents")
    def documents(request: Request):
        return listing(request, "document")

    @app.get("/api/changes")
    def changes(request: Request):
        return listing(request, "change_request")

    @app.post("/api/changes")
    async def create_change(request: Request):
        s = session(request)
        actor(s, {"dev01", "ops01", "dba01", "manager01"})
        body = await payload(request)
        with b._lock:
            ticket = text(body, "ticket", 80)
            application = find("ticket", ticket).payload["data"]["application"]
            n = 1
            while any(t["key"] == f"CHG-{n:04d}" for t in records("change_request")):
                n += 1
            return save(
                "change_request",
                f"CHG-{n:04d}",
                dict(
                    title=text(body, "title", 200),
                    ticket=ticket,
                    application=application,
                    owner=s["username"],
                    status="draft",
                    risk=body.get("risk", "medium"),
                    plan=text(body, "plan"),
                    rollback=text(body, "rollback"),
                    test_result=None,
                    approver=None,
                    deployment=None,
                    history=[],
                ),
                s,
                request.url.path,
            )

    @app.post("/api/changes/{key}/{action}")
    async def change_action(key: str, action: str, request: Request):
        s = session(request)
        body = await payload(request)
        rules = {
            "test": ({"test01"}, "draft", "tested"),
            "approve": ({"manager01"}, "tested", "approved"),
            "deploy": ({"ops01"}, "approved", "deployed"),
            "rollback": ({"ops01"}, "deployed", "rolled_back"),
        }
        if action not in rules:
            raise HTTPException(404, "操作不存在")
        allowed, source, target = rules[action]
        actor(s, allowed)
        with b._lock:
            data = deepcopy(find("change_request", key).payload["data"])
            if data["status"] != source:
                raise HTTPException(409, "当前状态不允许此操作")
            note = text(body, "note")
            if action == "test":
                if body.get("passed") is not True:
                    raise HTTPException(422, "测试未通过，不能进入审批")
                data["test_result"] = dict(passed=True, report=note, by=s["username"])
            if action == "approve":
                data["approver"] = s["username"]
            if action == "deploy":
                data["deployment"] = dict(
                    operator=s["username"],
                    result="success",
                    time=stamp(b.get_engine().world),
                    log=note,
                )
            data["status"] = target
            data["history"].append(
                dict(
                    action=action,
                    actor=s["username"],
                    note=note,
                    time=stamp(b.get_engine().world),
                )
            )
            return save(
                "change_request", key, data, s, request.url.path, body.get("version")
            )
