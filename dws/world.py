"""DWS 世界模型 —— 全系统的"物理定律"（契约 12 §2/§5；铁律 K6：语义冻结）。

- 追加式事实存储：每实体 version 严格 +1（I4 由构造保证，检测器兜底）
- CAS 单事务提交：base_version 校验 → 世界预算 → 增量不变量校验 → world_version+1
  失败回滚干净（单一写点、单线程，回滚 = 弹回本次追加的行）
- 会话钉定版本 + 自写叠加视图（View，契约 §5.1 v1.1）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .facts import ENTITY_KEYS, Fact, entity_uuid
from .hlc import HLC
from . import invariants as inv

# 世界预算（契约 §2.8；M4 前固定。校准只改这里，不改结构/语义）
# 预算只计"扩张类"实体——host/identity/credential/data/relation；
# timeline 是审计日志不是世界扩张，不占预算（否则每条事件都会烧掉预算额度）。
BUDGET_CLASSES = frozenset({"host", "identity", "credential", "data", "relation", "object"})
MAX_SESSION_NEW_ENTITIES = 12
MAX_WORLD_NODES = 200
MAX_SESSION_TOKENS = 40_000          # M0 无 LLM：恒 0，占位保持记账接口

SEED_VERSION = 1                     # 初始事实的 world_version


class CommitConflict(Exception):
    """CAS 失败：base_version 落后于当前世界版本（调用方重试一次后降级）。"""


class CommitRejected(Exception):
    """提案被裁决拒绝（不变量违反 / 世界预算超限）。reason 可读。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Session:
    session_id: str
    attacker_id: str
    user: Optional[str]
    pinned_version: int
    started_hlc: str
    host_entity_id: Optional[str]
    cwd: str = "/"
    own_versions: set = field(default_factory=set)   # 本会话提交的 world_version（自写叠加）
    new_entities: set = field(default_factory=set)   # 本会话新建实体（预算记账）
    tokens_used: int = 0


@dataclass
class CommitResult:
    ok: bool
    world_version: int
    error: Optional[str] = None


class World:
    """世界状态。唯一可写入口是 commit()（05 §02：AI 不能写事实，只能提案）。"""

    def __init__(self, seed: dict):
        meta = seed["meta"]
        node = meta.get("node", "replay")
        base_seconds = int(meta.get("base_seconds", 1_730_000_000))
        self.meta = {
            "world_id": meta.get("world_id", "world-001"),
            "seed": meta["seed"],
            "node": node,
            "subnets": list(meta.get("subnets", [])),
            "base_seconds": base_seconds,
            "world_version": max(SEED_VERSION, int(meta.get("initial_version", SEED_VERSION))),
            "now_hlc": f"{base_seconds}.0000/{node}",
        }
        self.facts: dict[str, dict[str, list[Fact]]] = {c: {} for c in ENTITY_KEYS}
        self.sessions: dict[str, Session] = {}
        self.cas_conflicts = 0
        self.commits_ok = 0
        self.commits_rejected = 0

        for cls, payloads in seed.get("initial_facts", {}).items():
            if cls not in ENTITY_KEYS:
                raise ValueError(f"unknown entity class in seed: {cls}")
            for payload in payloads:
                self._append_seed(cls, payload)

    # ------------------------------------------------------------- 读取视图

    def _append_seed(self, cls: str, payload: dict) -> None:
        f = Fact.new(cls, payload, version=1, hlc=self.meta["now_hlc"],
                     node_id=self.meta["node"], attacker_id=None,
                     world_version=self.meta["world_version"])
        self.facts[cls].setdefault(f.entity_id, []).append(f)

    def current(self, cls: str, entity_id: str) -> Optional[Fact]:
        rows = self.facts[cls].get(entity_id)
        return rows[-1] if rows else None

    def all_current_cls(self, cls: str) -> list[Fact]:
        """当前有效行视图（每实体 version 最大的一行）。"""
        return [rows[-1] for rows in self.facts[cls].values()]

    def total_nodes(self) -> int:
        return sum(len(entities) for cls, entities in self.facts.items()
                   if cls in BUDGET_CLASSES)

    def view(self, pinned_version: int, own_versions=()) -> "View":
        return View(self, pinned_version, own_versions)

    # ------------------------------------------------------------- 会话

    def open_session(self, session_id: str, attacker_id: str, user: Optional[str],
                     host_entity_id: Optional[str]) -> Session:
        """会话开始：钉定当前世界版本（契约 §5.1）。已存在的会话重复登录则刷新用户。"""
        sess = self.sessions.get(session_id)
        if sess is None:
            sess = Session(
                session_id=session_id, attacker_id=attacker_id, user=user,
                pinned_version=self.meta["world_version"],
                started_hlc=self.meta["now_hlc"],
                host_entity_id=host_entity_id)
            self.sessions[session_id] = sess
        else:
            sess.user = user
        return sess

    # ------------------------------------------------------------- 提交

    def commit(self, session_id: Optional[str], proposals: list,
               base_version: int, hlc: str, attacker_id: Optional[str] = None) -> CommitResult:
        """单事务提交。proposals = [(cls, payload), ...]；失败干净回滚。

        - CAS: base_version 必须等于当前 world_version，否则 CommitConflict
        - 预算: 会话新增扩张类实体 ≤ 12，世界总节点 ≤ 200（超限拒绝扩张，契约 §2.8）
        - 不变量: 对受影响实体增量校验（validate_commit），违反即拒绝
        - attacker_id: 无会话的提交（预认证事件）用显式传入的 attacker_id
        """
        if base_version != self.meta["world_version"]:
            self.cas_conflicts += 1
            raise CommitConflict(
                f"base_version {base_version} != world_version {self.meta['world_version']}")
        if not proposals:
            raise ValueError("empty commit")

        sess = self.sessions.get(session_id) if session_id else None
        attacker = sess.attacker_id if sess else attacker_id
        next_world = self.meta["world_version"] + 1

        # 世界时间不早于事务 hlc（引擎每事件已推进；直接调用 commit 的工具路径同样成立）
        if HLC.parse(hlc) > HLC.parse(self.meta["now_hlc"]):
            self.meta["now_hlc"] = hlc

        # 预算（先按"将引入的新实体"静态判定，再落行；timeline 不占预算）
        planned: list[tuple[str, str, int, dict]] = []
        distinct_new: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for cls, payload in proposals:
            if cls not in ENTITY_KEYS:
                raise CommitRejected(f"unknown entity class: {cls}")
            eid = entity_uuid(cls, ENTITY_KEYS[cls](payload))
            rows = self.facts[cls].get(eid)
            planned.append((cls, eid, rows[-1].version + 1 if rows else 1, payload))
            if rows is None and (cls, eid) not in seen:
                seen.add((cls, eid))
                if cls in BUDGET_CLASSES:
                    distinct_new.append((cls, eid))

        if sess is not None and distinct_new:
            if len(sess.new_entities) + len(distinct_new) > MAX_SESSION_NEW_ENTITIES:
                self.commits_rejected += 1
                raise CommitRejected(
                    f"budget: 会话新增实体 {len(sess.new_entities) + len(distinct_new)}"
                    f" > {MAX_SESSION_NEW_ENTITIES}")
        if self.total_nodes() + len(distinct_new) > MAX_WORLD_NODES:
            self.commits_rejected += 1
            raise CommitRejected(
                f"budget: 世界总节点 {self.total_nodes() + len(distinct_new)}"
                f" > {MAX_WORLD_NODES}")

        # 落行
        appended: list[Fact] = []
        for cls, eid, version, payload in planned:
            f = Fact.new(cls, payload, version=version, hlc=hlc,
                         node_id=self.meta["node"], attacker_id=attacker,
                         world_version=next_world)
            self.facts[cls].setdefault(eid, []).append(f)
            appended.append(f)

        # 不变量增量校验（违反即回滚）
        violations = inv.validate_commit(self, appended)
        if violations:
            self._rollback(appended)
            self.commits_rejected += 1
            raise CommitRejected("; ".join(str(v) for v in violations))

        self.meta["world_version"] = next_world
        if sess is not None:
            sess.own_versions.add(next_world)
            for cls, eid in distinct_new:
                sess.new_entities.add((cls, eid))
        self.commits_ok += 1
        return CommitResult(ok=True, world_version=next_world)

    def _rollback(self, appended: list[Fact]) -> None:
        for f in reversed(appended):
            rows = self.facts[f.cls][f.entity_id]
            rows.pop()
            if not rows:
                del self.facts[f.cls][f.entity_id]


class View:
    """钉定快照 + 自写叠加（契约 §5.1 v1.1）。

    可见性：fact.world_version ≤ pinned_version，或 ∈ 会话自身提交集合。
    效果：其他攻击者/异步任务引发的新世界对本会话不可见（防"新世界续写长会话"），
    而本会话自己的写入（echo > f 后 cat f）因果可见。
    """

    def __init__(self, world: World, pinned_version: int, own_versions=()):
        self.world = world
        self.pinned = pinned_version
        self.own = set(own_versions)

    def _visible(self, f: Fact) -> bool:
        return f.world_version <= self.pinned or f.world_version in self.own

    def current(self, cls: str, entity_id: str) -> Optional[Fact]:
        best = None
        for f in self.world.facts[cls].get(entity_id, ()):  # rows 按 version 升序
            if self._visible(f):
                best = f
        return best

    def all_current_cls(self, cls: str) -> list[Fact]:
        out = []
        for rows in self.world.facts[cls].values():
            best = None
            for f in rows:
                if self._visible(f):
                    best = f
            if best is not None:
                out.append(best)
        return out
