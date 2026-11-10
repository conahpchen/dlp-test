"""协作平台（MVP，契约 14 §04）：研发运维协作平台的业务 API + 前端 + 集成 AI 网关。

单一进程承载：业务 API（登录/工单/资产投影/审计）+ AI 网关端点（/v1/*，共享同一
Engine——业务与 LLM 入口世界强一致）+ 前端页面。

审计一致性（契约 12 §13.3）：所有用户（真人/Agent）经本服务的动作，审计行格式
完全一致，无任何 agent 标记；`entry=agent` 标注只存在于统一事件管道。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dws.ids import ulid_now
from dws.world import CommitRejected, World
from generation.llm_templates import classify_message
from judgment import L1RulesJudge
from replay.engine import Engine

REPO = Path(__file__).resolve().parents[1]

app = FastAPI(title="澄川研发服务平台", docs_url=None, redoc_url=None, openapi_url=None)
_lock = threading.RLock()  # 可重入：_audit 锁内调 get_engine（历史教训见 §13 运行约束）
_engine: Engine | None = None
_sessions: dict = {}  # token -> {username, identity_eid, role, cred_hash}
_audit_rows: list = []  # 审计行（格式对所有用户一致；无 agent 标记，契约 §13.3）
_events: list = []  # 统一事件（attacker/agent/business 三类来源）
_event_ids: set = set()

# 工单状态机（契约 12 §13.2 类型注册 ticket.constraints）
TICKET_FLOW = {
    "open": ["in_progress", "closed"],
    "in_progress": ["resolved", "closed"],
    "resolved": ["closed", "in_progress"],  # reopen
    "closed": [],
}


@app.middleware("http")
async def control_boundary(request: Request, call_next):
    # Collection and analyst data are control-plane interfaces, not employee pages.
    if request.url.path in ("/api/events", "/api/events/ingest"):
        expected = os.environ.get("HONEYNET_CONTROL_TOKEN", "")
        supplied = request.headers.get("x-control-token", "")
        if not expected or not secrets.compare_digest(expected, supplied):
            return JSONResponse({"error": "接口不存在"}, status_code=404)
    return await call_next(request)


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:40]


def get_engine() -> Engine:
    """惰性单例：世界种子来自 bundled fixtures 或 PG（可选）。"""
    global _engine
    with _lock:
        if _engine is not None:
            return _engine
        seed = None
        dsn = os.environ.get("HONEYNET_PG_DSN")
        if dsn:
            try:
                from dws import loader_sql

                psycopg = loader_sql.require_psycopg()
                with psycopg.connect(dsn) as conn:
                    seed = loader_sql.load_seed(conn)
            except Exception as e:  # noqa: BLE001 —— 降级路径要活着
                print("business: PG 世界加载失败，回退 bundled 种子")
        if seed is None:
            seed_path = os.environ.get(
                "GATEWAY_WORLD_SEED",
                str(REPO / "tests" / "fixtures" / "world_seed.demo.json"),
            )
            seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        from business.enterprise import enrich_seed

        candidate = Engine(World(enrich_seed(seed)))
        from business.storage import restore

        state = restore(candidate)
        if state:
            _audit_rows[:] = state["audit"]
            _events[:] = state["events"]
            _event_ids.update(e["event_id"] for e in _events)
        _engine = candidate
        return _engine


def _audit(user: str, method: str, path: str, status: int) -> None:
    """审计行：格式对所有用户完全一致（无 agent 标记，契约 12 §13.3）。"""
    ts = get_engine().world.meta["now_hlc"]  # 锁外取值（RLock 防重入兜底）
    with _lock:
        _audit_rows.append(
            {
                "seq": len(_audit_rows) + 1,
                "ts_hlc": ts,
                "user": user,
                "method": method,
                "path": path,
                "status": status,
            }
        )


def _emit(event: dict) -> None:
    """统一事件入管道（幂等）。"""
    with _lock:
        if event["event_id"] not in _event_ids:
            _event_ids.add(event["event_id"])
            _events.append(event)
            from business.storage import persist

            persist(get_engine(), _audit_rows, _events)


def _session_of(request: Request):
    auth = request.headers.get("authorization") or ""
    token = auth[7:] if auth.startswith("Bearer ") else ""
    return _sessions.get(token)


def _world_models(engine: Engine) -> list[str]:
    models = []
    for f in engine.world.all_current_cls("host"):
        for s in f.payload.get("services", []):
            if s.get("proto") == "openai" and s.get("ver"):
                models.append(s["ver"])
    return sorted(set(models))


def _biz_event(
    engine: Engine, username: str, cred_hash: str, path: str, correlation_id: str | None
) -> dict:
    return {
        "event_id": ulid_now(),
        "ts_hlc": engine.world.meta["now_hlc"],
        "entry": "web",
        "node": "business",
        "dst": {"host": "oa01", "port": 443},
        "session": f"oa-{username}",
        "attacker_id": None,
        "src": {"ip": "10.20.0.0/24"},
        "who": {"user": username, "cred_hash": cred_hash},
        "what": {"kind": "web.request", "method": "POST", "path": path},
        "verdict": None,
        "dws_version": engine.world.meta["world_version"],
        "evidence_ref": f"business://{username}/{len(_events) + 1}",
        "world_id": engine.world.meta.get("world_id"),
        "correlation_id": correlation_id,
    }


# ------------------------------------------------------------------ 认证


@app.post("/api/login")
async def login(request: Request):
    engine = get_engine()
    body = await request.json()
    username = body.get("username") or ""
    password = body.get("password") or ""
    cred_hash = _sha(password)

    cred = owner = None
    for c in engine.world.all_current_cls("credential"):
        p = c.payload
        if p["secret_hash"] != cred_hash or p["revoked_at_hlc"] is not None:
            continue
        own = engine.world.current("identity", p["owner_entity_id"])
        if own is None or own.payload["username"] != username:
            continue
        if any(v.get("service") == "oa" for v in p["valid_on"]):
            cred, owner = c, own
            break

    if cred is None:
        _audit(username or "-", "POST", "/api/login", 401)
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)

    token = secrets.token_urlsafe(24)
    with _lock:
        _sessions[token] = {
            "username": username,
            "identity_eid": owner.entity_id,
            "role": owner.payload["role"],
            "cred_hash": cred_hash,
        }
    _audit(username, "POST", "/api/login", 200)
    return {"token": token, "username": username, "role": owner.payload["role"]}


def _require(request: Request):
    sess = _session_of(request)
    if sess is None:
        raise PermissionError("未认证")
    return sess


# ------------------------------------------------------------------ 资产投影（DWS 只读）


@app.get("/api/assets")
def assets(request: Request):
    try:
        sess = _require(request)
    except PermissionError:
        return JSONResponse({"error": "未认证"}, status_code=401)
    engine = get_engine()
    view = engine.world.view(engine.world.meta["world_version"])
    hosts = [
        {
            "entity_id": f.entity_id,
            "hostname": f.payload["hostname"],
            "ip": f.payload["ip"],
            "os": f.payload["os"],
            "services": f.payload["services"],
        }
        for f in sorted(
            view.all_current_cls("host"), key=lambda x: x.payload["hostname"]
        )
    ]
    relations = [
        {
            "from": r.payload["from_entity_id"],
            "to": r.payload["to_entity_id"],
            "kind": r.payload["kind"],
            "ports": r.payload["ports"],
        }
        for r in view.all_current_cls("relation")
    ]
    _audit(sess["username"], "GET", "/api/assets", 200)
    return {"hosts": hosts, "relations": relations}


# ------------------------------------------------------------------ 公告（dws_object: announcement）


@app.get("/api/announcements")
def announcements(request: Request):
    try:
        sess = _require(request)
    except PermissionError:
        return JSONResponse({"error": "未认证"}, status_code=401)
    engine = get_engine()
    out = [
        {"key": f.payload["object_key"], **f.payload["data"]}
        for f in sorted(
            engine.world.all_current_cls("object"),
            key=lambda x: x.payload["object_key"],
        )
        if f.payload["type"] == "announcement"
    ]
    _audit(sess["username"], "GET", "/api/announcements", 200)
    return out


@app.post("/api/announcements")
async def announcement_create(request: Request):
    engine = get_engine()
    try:
        sess = _require(request)
    except PermissionError:
        return JSONResponse({"error": "未认证"}, status_code=401)
    body = await request.json()
    n_ann = len(
        [
            f
            for f in engine.world.all_current_cls("object")
            if f.payload["type"] == "announcement"
        ]
    )
    key = f"AN-{n_ann + 1:04d}"
    data = {
        "title": body.get("title") or "公告",
        "body": body.get("body") or "",
        "author": sess["username"],
    }
    proposal = ("object", {"type": "announcement", "object_key": key, "data": data})
    try:
        engine.commit_external(
            None, [proposal], correlation_id=key, actor_id=sess["username"]
        )
    except CommitRejected as e:
        return JSONResponse({"error": f"裁决拒绝: {e}"}, status_code=409)
    _audit(sess["username"], "POST", "/api/announcements", 200)
    return {"key": key}


# ------------------------------------------------------------------ 审计与事件


@app.get("/api/audit")
def audit_view(request: Request):
    try:
        sess = _require(request)
    except PermissionError:
        return JSONResponse({"error": "未认证"}, status_code=401)
    _audit(sess["username"], "GET", "/api/audit", 200)
    return {"rows": list(_audit_rows)}


@app.post("/api/events/ingest")
async def events_ingest(request: Request):
    """统一事件接入（内部；幂等）。"""
    text = (await request.body()).decode("utf-8")
    added = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        with _lock:
            if ev["event_id"] not in _event_ids:
                _event_ids.add(ev["event_id"])
                _events.append(ev)
                added += 1
    with _lock:
        from business.storage import persist

        persist(get_engine(), _audit_rows, _events)
    return {"ingested": added, "total": len(_events)}


@app.get("/api/events")
def events_view(request: Request):
    try:
        sess = _require(request)
    except PermissionError:
        return JSONResponse({"error": "未认证"}, status_code=401)
    return {"events": list(_events), "total": len(_events)}


# ------------------------------------------------------------------ 集成 AI 网关（共享 Engine，世界强一致）


@app.get("/v1/models")
def v1_models():
    engine = get_engine()
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "corp"}
            for m in _world_models(engine)
        ],
    }


def _world_models(engine: Engine) -> list[str]:
    models = []
    for f in engine.world.all_current_cls("host"):
        for s in f.payload.get("services", []):
            if s.get("proto") == "openai" and s.get("ver"):
                models.append(s["ver"])
    return sorted(set(models))


@app.post("/v1/chat/completions")
async def v1_chat(request: Request):
    """与 M1 网关同逻辑，但使用本进程共享 Engine（业务/LLM 世界强一致）。"""
    engine = get_engine()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {
                "error": {
                    "message": "Invalid JSON body",
                    "type": "invalid_request_error",
                }
            },
            400,
        )
    auth = request.headers.get("authorization") or ""
    key = auth[7:] if auth.startswith("Bearer ") else ""
    cred_hash = _sha(key) if key else None

    model = body.get("model") or ""
    if model not in _world_models(engine):
        return JSONResponse(
            {
                "error": {
                    "message": f"The model `{model}` does not exist",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            },
            404,
        )

    match = engine._match_llm_credential(cred_hash)
    messages = body.get("messages") or []
    last_user = next(
        (m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    target_class = classify_message(last_user, engine.judge.rules)
    session = request.headers.get("x-session") or f"oa-{(cred_hash or 'anon')[:12]}"
    client_ip = request.client.host if request.client else "0.0.0.0"
    parts = client_ip.split(".")
    if len(parts) == 4:
        client_ip = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"

    event = {
        "event_id": ulid_now(),
        "ts_hlc": engine.world.meta["now_hlc"],
        "entry": "llm",
        "node": "business",
        "dst": {"host": "ai01", "port": 4000},
        "session": session,
        "attacker_id": None,
        "src": {"ip": client_ip},
        "who": {"user": None, "cred_hash": cred_hash},
        "what": {
            "kind": "llm.chat",
            "model": model,
            "messages_hash": _sha(
                json.dumps(messages, ensure_ascii=False, sort_keys=True)
            ),
            "n_messages": len(messages),
            "last_role": messages[-1].get("role") if messages else None,
            "target_class": target_class,
        },
        "verdict": None,
        "dws_version": None,
        "evidence_ref": f"business://{session}/{len(messages)}",
        "world_id": engine.world.meta.get("world_id"),
        "correlation_id": None,
    }
    ctx = {
        "auth_failures": engine.auth_fails.get(
            engine.identity.observe(ip24=client_ip, cred_hash=cred_hash), 0
        ),
        "valid_key": engine.check_llm_key(cred_hash),
        "target_class": target_class,
    }
    event["verdict"] = engine.judge.judge_safe(event, ctx)
    with _lock:
        resp = engine.process(event)
        _emit(event)

    if resp.get("status") == "auth_fail":
        return JSONResponse(
            {
                "error": {
                    "message": "Incorrect API key provided.",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                }
            },
            401,
        )
    chat = resp["chat"]
    prompt_tokens = sum(len(str(m.get("content") or "")) // 4 + 1 for m in messages)
    return JSONResponse(
        {
            "id": f"chatcmpl-{resp['event_id']}",
            "object": "chat.completion",
            "created": 0,
            "model": chat["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": chat["content"]},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": chat["tokens"],
                "total_tokens": prompt_tokens + chat["tokens"],
            },
        }
    )


# ------------------------------------------------------------------ 前端页（MVP）


@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
def ui():
    tpl = Path(__file__).resolve().parent / "templates" / "index.html"
    return HTMLResponse(tpl.read_text(encoding="utf-8"))


from business.enterprise import register

register(app)
