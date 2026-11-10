"""SLO 度量（契约 12 §7）：从 harness metrics.json 聚合四项 SLO 的 M0 可测项。"""
from __future__ import annotations

# 目标值（12 §7）
TARGET_DEGRADATION_PER_MILLE = 50      # 降级率 ≤ 5%
TARGET_CAS_CONFLICT_PER_MILLE = 20     # CAS 冲突率 ≤ 2%


def aggregate(metrics: dict) -> dict:
    events = max(1, metrics.get("events", 0))
    commits = metrics.get("commits_ok", 0) + metrics.get("cas_conflicts", 0)
    return {
        "degradation_rate_per_mille": metrics.get("fallbacks", 0) * 1000 // events,
        "degradation_rate_target_per_mille": TARGET_DEGRADATION_PER_MILLE,
        "cas_conflict_rate_per_mille":
            metrics.get("cas_conflicts", 0) * 1000 // max(1, commits),
        "cas_conflict_rate_target_per_mille": TARGET_CAS_CONFLICT_PER_MILLE,
        "p95_gen_latency_ms": metrics.get("wall_ms_p95_event"),
        "p95_gen_latency_target_ms": {"template": 300, "llm": 1500},
        "projection_staleness_s": None,   # M0 单机无投影读；M5 起度量
        "note": "M0 为批处理近似（wall_ms 总量摊派）；在线路径按事件计时后替换",
    }
