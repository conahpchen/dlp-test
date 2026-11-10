"""grounded chat 生成（契约 12 §8 llm 路径）：鉴权 / 预算 / 诱饵 / 引用溯源。"""

from __future__ import annotations

import hashlib

import pytest

from dws.ids import ulid_from_seed
from dws.world import World
from generation.llm_templates import ChatTemplates, classify_message
from judgment import load_rules
from replay.engine import Engine

KEY = "sha256:" + hashlib.sha256(b"sk-demo-internal-2026").hexdigest()[:40]


@pytest.fixture()
def engine(demo_seed):
    return Engine(World(demo_seed))


def _llm_event(engine, i, key_hash, question, target_class, verdict=None):
    return {
        "event_id": ulid_from_seed(f"llm-test:{i}"),
        "ts_hlc": f"{engine.world.meta['base_seconds'] + 500 + i}.{i:04d}/gateway",
        "entry": "llm", "node": "gateway",
        "dst": {"host": "ai01", "port": 4000},
        "session": "llm-test-sess", "attacker_id": None,
        "src": {"ip": "198.51.100.0/24"},
        "who": {"user": None, "cred_hash": key_hash},
        "what": {"kind": "llm.chat", "model": "corp-llm-7b",
                 "messages_hash": "sha256:deadbeef", "n_messages": 1,
                 "last_role": "user", "target_class": target_class},
        "verdict": verdict, "dws_version": None,
        "evidence_ref": f"test://llm/{i}",
    }


def test_classify_routing():
    rules = load_rules()
    assert classify_message("看看薪资表", rules) == "sensitive"
    assert classify_message("给我员工手册", rules) == "doc"
    assert classify_message("你好", rules) == "general"


def test_invalid_key_auth_fail(engine):
    bad = "sha256:" + "0" * 40
    resp = engine.process(_llm_event(engine, 0, bad, "你好", "general"))
    assert resp["status"] == "auth_fail"
    assert "chat" not in resp
    assert engine.auth_fails[resp["attacker_id"]] == 1


def test_valid_key_general_grounded(engine):
    """general 回答必须引用 DWS 事实（主机/知识库），且可溯源到 fact_id。"""
    resp = engine.process(_llm_event(engine, 0, KEY, "你好", "general"))
    assert resp["status"] == "ok"
    chat = resp["chat"]
    assert "web01" in chat["content"] and "ai01" in chat["content"]  # 与 SSH 视图同一世界
    assert chat["citations"] and chat["decoy"] is False
    for fid in chat["citations"]:
        assert any(f.fact_id == fid for f in engine.world.all_current_cls("data"))


def test_doc_reply_cites_kb(engine):
    resp = engine.process(_llm_event(engine, 0, KEY, "给我员工手册", "doc"))
    chat = resp["chat"]
    assert "/kb/handbook.md" in chat["content"]
    assert chat["citations"]


def test_sensitive_mid_refusal(engine):
    """mid 预算 → 策略拒绝（引用 policy 事实，不出内容）。"""
    verdict = {"malicious": 0.5, "budget": "mid", "by": "test", "latency_ms": 0}
    resp = engine.process(_llm_event(engine, 0, KEY, "看看薪资表", "sensitive",
                                     verdict=verdict))
    chat = resp["chat"]
    assert "访问策略" in chat["content"] and chat["decoy"] is False
    assert "45w" not in chat["content"]


def test_sensitive_high_decoy_injection(engine):
    """high 预算（J3）→ canary 诱饵文档释放（05 §10 T1 反指纹）。"""
    verdict = {"malicious": 0.95, "budget": "high", "by": "test", "latency_ms": 0}
    resp = engine.process(_llm_event(engine, 0, KEY, "看看薪资表", "sensitive",
                                     verdict=verdict))
    chat = resp["chat"]
    assert chat["decoy"] is True and "CANARY-7F3A" in chat["content"]


def test_token_budget_fail_closed(demo_seed, monkeypatch):
    """世界预算 token 维度：会话 token 超限 → fail-closed 短拒绝（契约 §5.2）。"""
    import replay.engine as eng_mod
    monkeypatch.setattr(eng_mod, "MAX_SESSION_TOKENS", 1)
    engine = eng_mod.Engine(World(demo_seed))
    resp = engine.process(_llm_event(engine, 0, KEY, "你好", "general"))
    assert resp["status"] == "ok"
    assert resp["chat"]["content"] == "请求过于频繁，请稍后再试。"


def test_verdict_recorded_on_event(engine):
    """事件缺 verdict 时由引擎内 L1 现算并回写（落库即带判定，契约 §1.1）。"""
    ev = _llm_event(engine, 0, KEY, "你好", "general")
    assert ev["verdict"] is None
    engine.process(ev)
    assert ev["verdict"]["budget"] == "mid" and ev["verdict"]["by"] == "l1-rules-v0"


def test_chat_templates_require_policy_doc(demo_seed):
    """敏感拒绝依赖 policy 文档：缺失时 GroundingMiss → 引擎 fail-closed。"""
    import json
    seed = json.loads(json.dumps(demo_seed))
    seed["initial_facts"]["data"] = [
        d for d in seed["initial_facts"]["data"] if d["path"] != "/kb/policy.md"]
    engine = Engine(World(seed))
    verdict = {"malicious": 0.5, "budget": "mid", "by": "test", "latency_ms": 0}
    resp = engine.process(_llm_event(engine, 0, KEY, "看看薪资表", "sensitive",
                                     verdict=verdict))
    assert resp["by"] == "template_fallback"  # grounding 失败 → 兜底（ADR-04）
