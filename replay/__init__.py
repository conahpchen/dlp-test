"""回放 harness（契约 12 §6；11 §09 本周清单第 3 条）。

- canonical     规范序列化与逐字节一致口径
- engine        回放引擎（事件 → 世界转移 + grounded 响应）
- harness       CLI: run / verify / hash
- trajectories  种子驱动的随机轨迹生成（10^4 验收与消融实验的数据源）
- acceptance    G0 验收工具
"""
from __future__ import annotations
