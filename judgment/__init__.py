"""判定与交互预算（契约 12 §11）。

JudgeBackend 是唯一判定入口：L1 规则 v0 为内置确定性实现；llmrouter 级联
（L2 :4010 / L3 :8010，isotonic 校准 + conformal APS）实现同一接口即可替换，
不改任何其他代码（契约 §11.4）。
"""

from __future__ import annotations

from .base import JudgeBackend, L1RulesJudge, load_rules

__all__ = ["JudgeBackend", "L1RulesJudge", "load_rules"]
