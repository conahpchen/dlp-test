"""CSVR 报告渲染（契约 12 §4.3：一切 CSVR 必附 recall 区间，只报点值视为无效）。"""
from __future__ import annotations

from .csvr import score


def render(pack, config: dict | None = None) -> dict:
    """score() 结果 + 合规声明，可直接 JSON 落盘。"""
    result = score(pack, config)
    result["formula"] = "CSVR = sum_c w_c * |contradictions_c| / |interactions| (w_c = 0.25)"
    result["compliance"] = (
        "INVALID_FOR_PUBLICATION: recall 区间缺失（M2 双标注员 + capture-recapture 回填）"
        if result["recall_interval"] is None else "OK")
    return result
