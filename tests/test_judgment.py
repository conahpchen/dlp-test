"""判定 L1 规则 v0（契约 12 §11）：J1/J2/J3/默认 + fail-closed。"""

import pytest

from judgment import L1RulesJudge
from judgment.base import JudgeBackend

_LLM_EVENT = {"entry": "llm", "what": {"kind": "llm.chat", "target_class": "general"}}
_SSH_EVENT = {"entry": "ssh", "what": {"kind": "ssh.command"}}


def test_j3_sensitive_target_gets_decoy_budget():
    v = L1RulesJudge().judge(
        {"entry": "llm", "what": {"kind": "llm.chat", "target_class": "sensitive"}},
        {"valid_key": True, "auth_failures": 0, "target_class": "sensitive"})
    assert v["budget"] == "high" and v["by"] == "l1-rules-v0"


def test_j2_valid_key_mid():
    v = L1RulesJudge().judge(_LLM_EVENT,
                             {"valid_key": True, "auth_failures": 0,
                              "target_class": "general"})
    assert v["budget"] == "mid"


def test_default_low():
    v = L1RulesJudge().judge(_SSH_EVENT,
                             {"valid_key": False, "auth_failures": 0})
    assert v["budget"] == "low"


def test_j1_auth_fail_burst_high():
    v = L1RulesJudge().judge(_SSH_EVENT, {"valid_key": False, "auth_failures": 3})
    assert v["budget"] == "high" and v["malicious"] >= 0.9


class _Boom(JudgeBackend):
    name = "boom"

    def judge(self, event, ctx):
        raise RuntimeError("llmrouter 不可达")


def test_fail_closed_on_backend_error():
    """判定后端异常 → judge_safe 兜底 low 预算放行（宁可变笨不可变矛盾）。"""
    v = _Boom().judge_safe(_LLM_EVENT, {})
    assert v["budget"] == "low" and v["by"] == "fail-closed"
