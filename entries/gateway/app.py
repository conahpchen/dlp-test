"""AI 网关 :4000 —— OpenAI 兼容入口（契约 12 §9 v1.2；05 §07 接点①）。

POST /v1/chat/completions：
  1. Bearer key 即哈希 → who.cred_hash（明文 key 不落任何日志）；
  2. 消息原文只在入口侧结构化：classify_message → what.target_class（结构化字段）；
     messages_hash 只做关联，原文进 evidence_ref（05 §01 原则 5）；
  3. 判定（verdict；llmrouter 到位后替换 L1，契约 §11.4）→ engine 按预算 grounded 生成；
  4. 鉴权失败 → OpenAI 错误格式 401；未知模型 → 404 model_not_found。

GET /v1/models：假模型清单来自 DWS（ai01 主机 proto=openai 的 services.ver）。
世界加载：优先 Postgres（load_seed），失败回退 bundled 种子（诚实降级，不自由生成）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, Request

from dws import loader_sql
from dws.ids import ulid_now
from dws.world import World
from generation.llm_templates import classify_message
from judgment import L1RulesJudge
from replay.engine import Engine

REPO = Path(__file__).resolve().parents[2]
GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "ai01")
GATEWAY_PORT = 4000

app = FastAPI(title="ai-honeypot gateway", docs_url=None, redoc_url=None)
_lock = threading.Lock()
_engine: Engine | None = None


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:40]


def get_engine() -> Engine:
    """惰性单例：世界从 PG 加载，失败回退 bundled 种子（fail-closed 到模板池）。"""
    global _engine
    with _lock:
        if _engine is not None:
            return _engine
        seed = None
        dsn = os.environ.get("HONEYNET_PG_DSN")
        if dsn:
            try:
                psycopg = loader_sql.require_psycopg()
                with psycopg.connect(dsn) as conn:
                    seed = loader_sql.load_seed(conn)
            except Exception as e:  # noqa: BLE001 —— 降级路径要活着
                print(f"gateway: PG 世界加载失败，回退 bundled 种子: {e}")
        if seed is None:
            seed_path = os.environ.get(
                "GATEWAY_WORLD_SEED", str(REPO / "tests" / "fixtures" / "world_seed.demo.json"))
            seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        _engine = Engine(World(seed))
        return _engine


def _error(status: int, message: str, err_type: str, code: str | None = None) -> dict:
    return {"error": {"message": message, "type": err_type, "code": code}}


def respond(payload: dict, status: int = 200, xd: dict | None = None):
    """JSON 响应辅助：x-deception 审计字段随响应头走（含引用 fact_id / 诱饵标记 / 世界版本）。"""
    from fastapi.responses import JSONResponse
    headers = {"x-deception": json.dumps(xd, ensure_ascii=False)} if xd else None
    return JSONResponse(content=payload, status_code=status, headers=headers)


def _world_models(engine: Engine) -> list[str]:
    models = []
    for f in engine.world.all_current_cls("host"):
        for s in f.payload.get("services", []):
            if s.get("proto") == "openai" and s.get("ver"):
                models.append(s["ver"])
    return sorted(set(models))


def _build_llm_event(engine: Engine, key_hash: str, body: dict, client_ip: str,
                     session: str, target_class: str, valid_key: bool) -> dict:
    """构造统一事件（契约 12 §1.2 llm.chat）。"""
    messages = body.get("messages") or []
    last_role = messages[-1].get("role") if messages else None
    event = {
        "event_id": ulid_now(),
        "ts_hlc": f"{engine.world.meta['base_seconds']}.0000/gateway",   # 引擎时钟会吸收推进
        "entry": "llm",
        "node": "gateway",
        "dst": {"host": GATEWAY_HOST, "port": GATEWAY_PORT},
        "session": session,
        "attacker_id": None,
        "src": {"ip": client_ip},
        "who": {"user": None, "cred_hash": key_hash},
        "what": {"kind": "llm.chat",
                 "model": body.get("model") or "",
                 "messages_hash": _sha(json.dumps(messages, ensure_ascii=False, sort_keys=True)),
                 "n_messages": len(messages),
                 "last_role": last_role,
                 "target_class": target_class},
        "verdict": None,
        "dws_version": None,
        "evidence_ref": f"gateway://{session}/{len(messages)}",
    }
    # 判定（契约 §11）：网关侧预判 → 事件携带 verdict 落库；引擎内缺省兜底重算
    ctx = {"auth_failures": engine.auth_fails.get(
               engine.identity.observe(ip24=event["src"]["ip"], cred_hash=key_hash), 0),
           "valid_key": valid_key,
           "target_class": target_class}
    event["verdict"] = engine.judge.judge(event, ctx)
    return event


@app.get("/healthz")
def healthz():
    engine = get_engine()
    return {"ok": True, "world_version": engine.world.meta["world_version"]}


@app.get("/v1/models")
def list_models():
    engine = get_engine()
    return {"object": "list",
            "data": [{"id": m, "object": "model", "owned_by": "corp"} for m in _world_models(engine)]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    engine = get_engine()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return respond(_error(400, "Invalid JSON body", "invalid_request_error"), 400)

    auth = request.headers.get("authorization") or ""
    key = auth[7:] if auth.startswith("Bearer ") else ""
    key_hash = _sha(key) if key else None

    models = _world_models(engine)
    model = body.get("model") or ""
    if model not in models:
        return respond(_error(404, f"The model `{model or '(empty)'}` does not exist",
                              "invalid_request_error", "model_not_found"), 404)

    messages = body.get("messages") or []
    last_user = next((m.get("content") or "" for m in reversed(messages)
                      if m.get("role") == "user"), "")
    target_class = classify_message(last_user, engine.judge.rules)
    session = request.headers.get("x-session") or f"gw-{(key_hash or 'anon')[:12]}"
    client_ip = request.client.host if request.client else "0.0.0.0"
    # /24 截断（合规 T6）
    parts = client_ip.split(".")
    if len(parts) == 4:
        client_ip = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"

    valid_key = engine.check_llm_key(key_hash)
    event = _build_llm_event(engine, key_hash, body, client_ip, session,
                             target_class, valid_key)
    resp = engine.process(event)

    # 事件落库（PG 可用即落；不可用只影响持久化，不影响响应）
    dsn = os.environ.get("HONEYNET_PG_DSN")
    if dsn:
        try:
            psycopg = loader_sql.require_psycopg()
            with psycopg.connect(dsn) as conn:
                loader_sql.insert_event(conn, event)
        except Exception as e:  # noqa: BLE001
            print(f"gateway: 事件落库失败（不影响响应）: {e}")

    if resp.get("status") == "auth_fail":
        return respond(_error(401, "Incorrect API key provided.",
                              "authentication_error", "invalid_api_key"), 401)

    chat = resp["chat"]
    prompt_tokens = sum(len(str(m.get("content") or "")) // 4 + 1 for m in messages)
    xd = {"citations": chat["citations"], "decoy": chat["decoy"],
          "budget": chat["budget"], "by": chat["verdict_by"],
          "dws_version": resp["dws_version"], "attacker_id": resp["attacker_id"]}
    return respond({
        "id": f"chatcmpl-{resp['event_id']}",
        "object": "chat.completion",
        "created": 0,
        "model": chat["model"],
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": chat["content"]},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": chat["tokens"],
                  "total_tokens": prompt_tokens + chat["tokens"]},
    }, 200, xd)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=GATEWAY_PORT)
