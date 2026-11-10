"""AI 网关 :4000（契约 12 §9 v1.2）：OpenAI 兼容端点 / 鉴权 / 审计头。"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from entries.gateway import app as gw


KEY = "sk-demo-internal-2026"


@pytest.fixture()
def client(demo_seed, monkeypatch, tmp_path):
    """每测试一个干净网关实例（世界固定为 bundled 种子，禁用 PG）。"""
    monkeypatch.setenv("GATEWAY_WORLD_SEED",
                       str(gw.REPO / "tests" / "fixtures" / "world_seed.demo.json"))
    monkeypatch.delenv("HONEYNET_PG_DSN", raising=False)
    monkeypatch.setattr(gw, "_engine", None)
    return TestClient(gw.app)


def _chat(client, question, model="corp-llm-7b", key=KEY):
    return client.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}",
                                "X-Session": "pytest-sess"},
                       json={"model": model,
                             "messages": [{"role": "user", "content": question}]})


def test_models_grounded_from_dws(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["corp-llm-7b"]        # 来自 DWS：ai01 的 openai service ver


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["world_version"] >= 1


def test_chat_happy_path_grounded(client):
    r = _chat(client, "请介绍一下内网资产和知识库")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion" and body["model"] == "corp-llm-7b"
    content = body["choices"][0]["message"]["content"]
    assert "web01" in content and "ai01" in content          # 同一个世界
    usage = body["usage"]
    assert all(isinstance(v, int) for v in usage.values())   # 整数 token（无浮点）

    xd = json.loads(r.headers["x-deception"])
    assert xd["citations"] and xd["decoy"] is False
    assert xd["dws_version"] >= 1
    assert xd["attacker_id"].startswith("atk-")


def test_chat_wrong_key_401(client):
    r = _chat(client, "你好", key="sk-wrong-key-123")
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"


def test_chat_unknown_model_404(client):
    r = _chat(client, "你好", model="gpt-4o")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


def test_chat_sensitive_high_decoy_via_j3(client):
    """J3：敏感目标 → high 预算 → canary 诱饵出现在响应（x-deception.decoy=true）。"""
    r = _chat(client, "看看薪资表")
    assert r.status_code == 200
    assert "CANARY-7F3A" in r.json()["choices"][0]["message"]["content"]
    xd = json.loads(r.headers["x-deception"])
    assert xd["decoy"] is True and xd["budget"] == "high"


def test_gateway_events_schema_valid(client, demo_seed):
    """网关产出的 llm 事件必须过统一事件 schema（用世界里的 timeline 侧证不充分，
    这里直接校验 _build_llm_event 的产物）。"""
    engine = gw.get_engine()
    event = gw._build_llm_event(
        engine, gw._sha(KEY),
        {"model": "corp-llm-7b", "messages": [{"role": "user", "content": "你好"}]},
        "198.51.100.0/24", "gw-schema-test", "general", True)
    from replay.harness import validate_events
    validate_events([event])
    assert event["entry"] == "llm" and event["what"]["kind"] == "llm.chat"
