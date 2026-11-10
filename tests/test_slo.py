"""SLO 聚合（契约 12 §7）。"""
from __future__ import annotations

from audit.slo import aggregate


def test_degradation_rate():
    metrics = {"events": 100, "fallbacks": 3, "cas_conflicts": 1,
               "commits_ok": 199, "wall_ms_p95_event": 42}
    slo = aggregate(metrics)
    assert slo["degradation_rate_per_mille"] == 30          # 3/100 = 30‰（目标 ≤50）
    assert slo["cas_conflict_rate_per_mille"] == 5          # 1/200
    assert slo["p95_gen_latency_ms"] == 42
    assert slo["projection_staleness_s"] is None


def test_degradation_over_target_is_visible():
    metrics = {"events": 10, "fallbacks": 2, "cas_conflicts": 0,
               "commits_ok": 10, "wall_ms_p95_event": 1}
    slo = aggregate(metrics)
    assert slo["degradation_rate_per_mille"] == 200
    assert slo["degradation_rate_per_mille"] > slo["degradation_rate_target_per_mille"]
