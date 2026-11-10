"""身份多视图合并 v1（契约 12 §2.3a，M1）。

并查集合并 `ip:<ip24>` 与 `cred:<cred_hash>` 两类视图；簇标识 = 簇内最早出现的
视图键（合并时旧簇吞并新簇）→ attacker_id 在整条回放序列内稳定。

- 同一 IP 的 SSH 与 LLM 会话天然同簇（G1 pivot 前提）
- 共享凭据的不同 IP 也归并（多视图价值）
- 确定性：合并轨迹由输入序列决定，同一输入 → 同一 attacker_id 序列（K5）
- JA3 / 行为 / 时间窗视图为 M3 扩展点，observe() 接口不变
"""

from __future__ import annotations

import hashlib


class MultiViewResolver:
    def __init__(self):
        self._parent: dict = {}
        self._seq: dict = {}        # 视图键 -> 首次出现序号（确定性"早晚"）
        self._root_key: dict = {}   # 簇根 -> 簇内最早的视图键
        self.edges: list = []       # 合并边记录（identity_edge 骨架，契约 §11.3）

    def _find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # 路径折半
            x = self._parent[x]
        return x

    def _ensure(self, view: str) -> str:
        self._parent.setdefault(view, view)
        if view not in self._seq:
            self._seq[view] = len(self._seq)
        root = self._find(view)
        if root not in self._root_key:
            self._root_key[root] = view
        return root

    def _union(self, a: str, b: str) -> str:
        ra, rb = self._find(a), self._find(b)
        if ra == rb:
            return ra
        ka, kb = self._root_key[ra], self._root_key[rb]
        if self._seq[ka] <= self._seq[kb]:      # 旧簇吞并新簇
            self._parent[rb] = ra
            self._root_key.pop(rb, None)
            self.edges.append({"view_a": ka, "view_b": kb, "signal": "observe-merge"})
            return ra
        self._parent[ra] = rb
        self._root_key.pop(ra, None)
        self.edges.append({"view_a": kb, "view_b": ka, "signal": "observe-merge"})
        return rb

    def observe(self, ip24: str | None = None, cred_hash: str | None = None) -> str:
        """输入一条事件的视图观测，返回该簇的稳定 attacker_id。"""
        views = []
        if ip24:
            views.append(f"ip:{ip24}")
        if cred_hash:
            views.append(f"cred:{cred_hash}")
        if not views:
            views = ["ip:unknown"]
        root = self._ensure(views[0])
        for v in views[1:]:
            root = self._union(root, self._ensure(v))
        key = self._root_key[self._find(root)]
        return "atk-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
