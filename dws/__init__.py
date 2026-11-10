"""DWS —— Deception World State 的语义核心与持久化（契约 12 §2/§3/§5）。

包内模块:
- ids        确定性 ID 推导（回放逐字节一致的前提）
- hlc        混合逻辑时钟
- facts      追加式版本化事实
- world      世界模型（CAS 提交 / 会话钉版本 / 世界预算）
- invariants 四条不变量断言（I1–I4）
- loader_sql Postgres 装载（内网部署用；Windows 无 Docker 时仅跳过集成测试）

语义冻结（K6）: 本包的世界转移语义自 M0 起不得修改，只允许追加。
"""
from __future__ import annotations

ENTITY_CLASSES = ("host", "identity", "credential", "data", "relation", "timeline", "object")
