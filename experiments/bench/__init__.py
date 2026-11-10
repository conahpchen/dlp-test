"""M2 度量篇 benchmark：同脚本回放框架。

- script    系统无关的固定攻击脚本（四套蜜网喂同一份）
- runner    按系统编排：drive（可选，活蜜罐）→ parse_logs → 统一事件 → CSVR 报告
- adapters  各蜜网的驱动与日志解析（cowrie 可用；tpot/galah/opencanary 骨架，部署后补）
- ingest    CLI：外部公开数据集（cowrie json 日志）过同一 scorer

G2 判据：κ ≥ 0.6 且 4 套系统的 CSVR 可对比（12 §4.3 / 11 §04）。
"""
