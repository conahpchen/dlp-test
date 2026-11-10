"""判定后端（契约 12 §11）。

- L1RulesJudge：确定性规则 v0（J1 暴破嫌疑 / J2 llm 有效 key / J3 敏感目标诱饵）。
- 判定异常 → fail-closed 按 low 预算放行（宁可变笨不可变矛盾，ADR-04）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "contracts" / "judgment_rules.yaml"


def load_rules() -> dict:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


class JudgeBackend:
    """判定后端接口（llmrouter 实现同一接口即可替换）。

    调用方必须走 judge_safe()——fail-closed 在调用点统一兜底，
    任何后端的异常都不会打穿引擎（契约 §11.5）。
    """

    name = "backend"

    def judge(self, event: dict, ctx: dict) -> dict:
        raise NotImplementedError

    def judge_safe(self, event: dict, ctx: dict) -> dict:
        try:
            return self.judge(event, ctx)
        except Exception as e:  # noqa: BLE001 —— fail-closed 是契约要求
            return self._fail_closed(f"judge backend error: {e}")

    @staticmethod
    def _fail_closed(reason: str) -> dict:
        return {"malicious": 0.0, "budget": "low", "by": "fail-closed",
                "latency_ms": 0, "note": reason}


class L1RulesJudge(JudgeBackend):
    """L1 规则版 v0：确定性、无模型（契约 §11.3 预注册规则清单）。"""

    name = "l1-rules-v0"

    def __init__(self, rules: dict | None = None):
        self.rules = rules or load_rules()

    def judge(self, event: dict, ctx: dict) -> dict:
        return self._judge(event, ctx)

    def _judge(self, event: dict, ctx: dict) -> dict:
        budget = self.rules["default_budget"]
        malicious = 0.3

        if ctx.get("auth_failures", 0) >= int(self.rules["auth_fail_threshold"]):
            budget = "high"                       # J1 暴破嫌疑
            malicious = 0.9
        if event["entry"] == "llm" and ctx.get("valid_key"):
            budget = self.rules["llm_valid_key_budget"]   # J2
            malicious = max(malicious, 0.5)
        if (event["what"].get("kind") == "llm.chat"
                and ctx.get("target_class") == "sensitive"):
            budget = "high"                       # J3 敏感目标 → 诱饵注入
            malicious = max(malicious, 0.95)

        return {"malicious": malicious, "budget": budget,
                "by": self.name, "latency_ms": 0}
