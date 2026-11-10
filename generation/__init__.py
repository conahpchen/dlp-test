"""生成层（契约 12 §8）。

M0 只走模板路径（六步流水线的 1、2、4、5：检索事实 → 填模板 → 一致性自检 → 泄漏守卫），
LLM 措辞层（第 3 步）M1 接入。铁律 K1：事实只来自 DWS，模板只做格式化，不编造世界。

GroundingMiss = 生成层需要的事实缺失。引擎捕获后走 fail-closed 兜底模板
（by=template_fallback，计入 SLO 降级率），绝不自由发挥（ADR-02/04）。
"""
from __future__ import annotations


class GroundingMiss(Exception):
    """grounding 失败：模板所需事实在（钉定视图的）世界中不存在。"""
