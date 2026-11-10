# judgment —— 判定与策略（M1 起填充）

llmrouter 级联判定接入（05 §07）：L1 规则 → L2 LightGBM+语义（isotonic 校准）→
L3 LoRA judge（仅疑难样本）；conformal APS (α=0.05)；输出从 tier 改为**交互预算**（低/中/高）。

- 端点契约：classifier :4010 / judge :8010（12 §9）
- 预算映射：预测集单元素 → 低；多候选 → 中；疑似定向/跨入口 pivot → 高
- M0 阶段本目录为空：M0 无判定器，`verdict = null`（契约 §1.1）
