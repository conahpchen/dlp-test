"""标注工具（M2，契约 12 §4.3）：双人独立盲标 T/C/M/I/无矛盾。

- 两位标注员（a/b）对同一批会话转写各自独立标注，互相不可见（盲标）；
- sqlite 存储（stdlib，零额外依赖）；标注结果导出后用 scorer.kappa 计算 κ
  与 capture–recapture recall 区间；
- 部署：uvicorn experiments.labeling.app:app --port 8300（仅内网/localhost）。

启动前先导入待标转写：POST /api/import（JSONL，每行一个 session bundle，
由 experiments/labeling/bundles.py 生成）。
"""

from __future__ import annotations
