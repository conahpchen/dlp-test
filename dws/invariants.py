"""四条不变量（契约 12 §3）。

纪律：先有断言、后有实现（11 §09）。检测器必须对注入的坏状态报警
（tests/test_invariants.py 逐条构造违例），对合法提交路径零误报
（10^4 随机轨迹 0 违反 = G0 第三判据，见 replay/acceptance.py）。

语义：断言针对世界快照（当前有效行视图）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .facts import ENTITY_KEYS, entity_uuid
from .hlc import HLC


@dataclass
class InvariantViolation:
    rule: str          # "I1".."I4"
    entity_id: str | None
    detail: str

    def __str__(self):
        loc = f" entity={self.entity_id}" if self.entity_id else ""
        return f"{self.rule}:{loc} {self.detail}"


# ---------------------------------------------------------------- I1 时间线单调

def check_i1(world) -> list[InvariantViolation]:
    """∀ attacker 的 timeline 按 world_version 序 hlc 严格递增；
    且 ∀ data 当前行：ctime ≤ mtime ≤ now。"""
    viols: list[InvariantViolation] = []

    per_attacker: dict[str, list] = {}
    for rows in world.facts["timeline"].values():
        f = rows[-1]
        per_attacker.setdefault(f.attacker_id, []).append(f)
    for attacker, rows in per_attacker.items():
        rows.sort(key=lambda f: f.world_version)
        prev_key = None
        for f in rows:
            k = HLC.parse(f.hlc).key()
            if prev_key is not None and k <= prev_key:
                viols.append(InvariantViolation(
                    "I1", f.entity_id,
                    f"attacker={attacker} timeline hlc 非严格递增: {f.hlc}"))
            prev_key = k

    now = HLC.parse(world.meta["now_hlc"])
    for rows in world.facts["data"].values():
        f = rows[-1]
        ctime = HLC.parse(f.payload["ctime_hlc"])
        mtime = HLC.parse(f.payload["mtime_hlc"])
        if not (ctime <= mtime):
            viols.append(InvariantViolation(
                "I1", f.entity_id,
                f"path={f.payload['path']} mtime({mtime}) 早于 ctime({ctime})"))
        if not (mtime <= now):
            viols.append(InvariantViolation(
                "I1", f.entity_id,
                f"path={f.payload['path']} mtime({mtime}) 晚于 now({now})"))
    return viols


# ------------------------------------------------------------ I2 凭据单一有效

def _has_revocation_event(world, credential_entities: set[str], now: HLC) -> bool:
    for rows in world.facts["timeline"].values():
        f = rows[-1]
        if f.payload.get("kind") != "revocation":
            continue
        ref = f.payload.get("object_ref", "")
        for eid in credential_entities:
            if eid in ref and HLC.parse(f.hlc) <= now:
                return True
    return False


def _scope_consistent(prev: dict, cur: dict) -> bool:
    """同一凭据实体相邻两版本的作用域/吊销状态是否一致。"""
    return (frozenset(v["host"] for v in prev["valid_on"])
            == frozenset(v["host"] for v in cur["valid_on"])
            and (prev["revoked_at_hlc"] is None) == (cur["revoked_at_hlc"] is None))


def check_i2(world) -> list[InvariantViolation]:
    """凭据单一有效（世界内可判定形）：同一凭据实体相邻版本间作用域/吊销状态的
    任何变化，必须存在显式吊销事件（≤ now）解释——不允许凭据"悄悄失效"。
    （跨入口的 C-1/C-2 矛盾由 scorer 评估；密码跨属主复用是合法现实，不在此列。）"""
    viols: list[InvariantViolation] = []
    now = HLC.parse(world.meta["now_hlc"])
    for rows in world.facts["credential"].values():
        if len(rows) < 2:
            continue
        prev, cur = rows[-2], rows[-1]
        if not _scope_consistent(prev.payload, cur.payload) and not _has_revocation_event(
                world, {cur.entity_id}, now):
            viols.append(InvariantViolation(
                "I2", cur.entity_id,
                f"secret {cur.payload['secret_hash'][:16]}… 作用域/吊销变化 "
                f"{prev.payload['valid_on']} -> {cur.payload['valid_on']} 无吊销事件"))
    return viols


# -------------------------------------------------------------- I3 可达性闭包

def check_i3(world) -> list[InvariantViolation]:
    """∀ relation: ports ⊆ services(to)。目标主机缺失同样违例。"""
    viols: list[InvariantViolation] = []
    for f in world.all_current_cls("relation"):
        to_fact = world.current("host", f.payload["to_entity_id"])
        if to_fact is None:
            viols.append(InvariantViolation(
                "I3", f.entity_id, "relation 指向不存在的主机"))
            continue
        svc_ports = {s["port"] for s in to_fact.payload["services"]}
        missing = set(f.payload["ports"]) - svc_ports
        if missing:
            viols.append(InvariantViolation(
                "I3", f.entity_id,
                f"声称端口 {sorted(missing)} 不在目标 {to_fact.payload['hostname']} 的 services 中"))
    return viols


# ---------------------------------------------------------------- I4 版本单调

def check_i4(world) -> list[InvariantViolation]:
    """∀ entity: version 严格 +1 连续无空洞；world_version 沿版本序严格递增；
    全局 world_version ≥ 任意事实的 world_version。"""
    viols: list[InvariantViolation] = []
    max_fv = 0
    for cls, entities in world.facts.items():
        for eid, rows in entities.items():
            for i, f in enumerate(rows, start=1):
                max_fv = max(max_fv, f.world_version)
                if f.version != i:
                    viols.append(InvariantViolation(
                        "I4", eid, f"{cls} version 断裂: 期望 {i} 实得 {f.version}"))
                if i > 1 and not f.world_version > rows[i - 2].world_version:
                    viols.append(InvariantViolation(
                        "I4", eid, f"{cls} world_version 沿版本序非递增"))
    if world.meta["world_version"] < max_fv:
        viols.append(InvariantViolation(
            "I4", None,
            f"全局 world_version {world.meta['world_version']} < 事实最大 world_version {max_fv}"))
    return viols


def check_all(world) -> list[InvariantViolation]:
    return check_i1(world) + check_i2(world) + check_i3(world) + check_i4(world)


# ------------------------------------------------ 提交时增量校验（受影响实体）

def validate_commit(world, appended: list) -> list[InvariantViolation]:
    """对一次提交涉及到的实体做增量校验（全量 check_all 太慢，提交路径只查受影响面）。

    语义与全量检查完全一致——同一套判定函数的两个入口。
    """
    viols: list[InvariantViolation] = []
    now = HLC.parse(world.meta["now_hlc"])
    host_touched: set[str] = set()
    cred_touched: set[str] = set()

    for f in appended:
        if f.cls == "timeline":
            # 同 attacker 的上一条 timeline（world_version 更小者中最大）
            prev = None
            for rows in world.facts["timeline"].values():
                cand = rows[-1]
                if (cand.attacker_id == f.attacker_id
                        and cand.world_version < f.world_version
                        and (prev is None or cand.world_version > prev.world_version)):
                    prev = cand
            if prev is not None and not HLC.parse(prev.hlc) < HLC.parse(f.hlc):
                viols.append(InvariantViolation(
                    "I1", f.entity_id,
                    f"attacker={f.attacker_id} timeline hlc 非严格递增: "
                    f"{prev.hlc} -> {f.hlc}"))
        elif f.cls == "data":
            ctime = HLC.parse(f.payload["ctime_hlc"])
            mtime = HLC.parse(f.payload["mtime_hlc"])
            if not (ctime <= mtime <= now):
                viols.append(InvariantViolation(
                    "I1", f.entity_id,
                    f"path={f.payload['path']} 时间越界 ctime={ctime} mtime={mtime} now={now}"))
        elif f.cls == "credential":
            cred_touched.add(f.entity_id)
        elif f.cls == "relation":
            viols.extend(_relation_ok(world, f))
        elif f.cls == "host":
            host_touched.add(f.entity_id)

    for eid in host_touched:
        for f in world.all_current_cls("relation"):
            if f.payload["to_entity_id"] == eid:
                viols.extend(_relation_ok(world, f))
    for eid in cred_touched:
        rows = world.facts["credential"].get(eid, [])
        if len(rows) >= 2 and not _scope_consistent(rows[-2].payload, rows[-1].payload) \
                and not _has_revocation_event(world, {eid}, now):
            viols.append(InvariantViolation(
                "I2", eid,
                f"secret {rows[-1].payload['secret_hash'][:16]}… 作用域/吊销变化无吊销事件"))
    return viols


def _relation_ok(world, rel) -> list[InvariantViolation]:
    to_fact = world.current("host", rel.payload["to_entity_id"])
    if to_fact is None:
        return [InvariantViolation("I3", rel.entity_id, "relation 指向不存在的主机")]
    svc_ports = {s["port"] for s in to_fact.payload["services"]}
    missing = set(rel.payload["ports"]) - svc_ports
    if missing:
        return [InvariantViolation(
            "I3", rel.entity_id,
            f"声称端口 {sorted(missing)} 不在目标 {to_fact.payload['hostname']} 的 services 中")]
    return []
