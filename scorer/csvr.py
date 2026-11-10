"""CSVR v0 判定规则（契约 12 §4.2 的 T-1..I-1；规则配置 contracts/csvr_rules.yaml）。

输入是证据包 EvidencePack：每入口一份世界视图 + 各入口的交互观测。
v0 判定全部纯函数、整数运算（无浮点）——与回放同一确定性纪律。

申报约束（契约 §4.3）：结果只报点值视为无效——report 必须带 recall 区间占位，
v0 阶段 valid=False（recall 区间 M2 由两名标注员 + capture–recapture 回填）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "contracts" / "csvr_rules.yaml"


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


@dataclass
class AuthOutcome:
    entry: str
    secret_hash: str
    host: str
    success: bool


@dataclass
class DataObservation:
    entry: str
    path: str
    ctime_hlc: str
    mtime_hlc: str


@dataclass
class RoleGrant:
    entry: str
    attacker_id: str
    role: str


@dataclass
class ReachObservation:
    entry: str
    from_host: str
    to_host: str
    success: bool


@dataclass
class EvidencePack:
    """每入口一份世界视图（World 或 View）+ 跨入口交互观测。"""
    worlds: dict = field(default_factory=dict)          # entry -> World/View
    auth: list = field(default_factory=list)            # [AuthOutcome]
    data: list = field(default_factory=list)            # [DataObservation]
    roles: list = field(default_factory=list)           # [RoleGrant]
    reach: list = field(default_factory=list)           # [ReachObservation]


def _hlc_ticks(hlc_str: str) -> int:
    from dws.hlc import HLC
    s, c, _ = HLC.parse(hlc_str).key()
    return s * 10_000 + c


def score(pack: EvidencePack, config: dict | None = None) -> dict:
    """评估七条规则，返回可序列化结果（整数运算）。"""
    cfg = config or load_config()
    counts = {rid: 0 for rid in cfg["rules"]}
    entries = sorted(pack.worlds)
    single = len(entries) < 2

    # T-1 / T-2: 拓扑（跨入口；单入口时跨规则恒 0）
    if not single:
        a, b = entries[0], entries[1]
        wa, wb = pack.worlds[a], pack.worlds[b]
        for o in pack.reach:
            same_pair_fail = (o.entry == a and not o.success)
            other_ok = any(x.entry == b and x.from_host == o.from_host
                           and x.to_host == o.to_host and x.success
                           for x in pack.reach)
            if same_pair_fail and other_ok:
                counts["T-1"] += 1
            in_b = any(f.payload["hostname"] == o.to_host
                       for f in wb.all_current_cls("host"))
            in_a = any(f.payload["hostname"] == o.to_host
                       for f in wa.all_current_cls("host"))
            if in_b and not in_a and o.entry == a:
                counts["T-2"] += 1

    # C-1: 同一 secret 在入口 A 成功、在另一入口失败（跨入口对；同入口翻转不计）
    by_secret: dict[str, list] = {}
    for o in pack.auth:
        by_secret.setdefault(o.secret_hash, []).append(o)
    for outcomes in by_secret.values():
        oks = [o for o in outcomes if o.success]
        fails = [o for o in outcomes if not o.success]
        counts["C-1"] += sum(1 for a in oks for b in fails if a.entry != b.entry)

    # C-2: 同一 secret 两入口吊销视图分歧
    if not single:
        seen: dict[str, dict[str, bool]] = {}
        for entry in entries:
            for f in pack.worlds[entry].all_current_cls("credential"):
                s = f.payload["secret_hash"]
                seen.setdefault(s, {})[entry] = f.payload["revoked_at_hlc"] is not None
        for s, views in seen.items():
            if len(set(views.values())) > 1:
                counts["C-2"] += 1

    # M-1: data 时间越界（mtime < ctime 或 > 世界 now）
    for o in pack.data:
        if _hlc_ticks(o.mtime_hlc) < _hlc_ticks(o.ctime_hlc):
            counts["M-1"] += 1
            continue
        w = pack.worlds.get(o.entry)
        if w is not None and _hlc_ticks(o.mtime_hlc) > _hlc_ticks(w.meta["now_hlc"]):
            counts["M-1"] += 1

    # M-2: 同一路径跨入口 mtime 倒挂 > ε
    eps = int(cfg["epsilon_hlc_ticks"])
    by_path: dict[str, dict[str, str]] = {}
    for o in pack.data:
        by_path.setdefault(o.path, {})[o.entry] = o.mtime_hlc
    for path, views in by_path.items():
        vals = list(views.values())
        if len(vals) >= 2 and abs(_hlc_ticks(vals[0]) - _hlc_ticks(vals[1])) > eps:
            counts["M-2"] += 1

    # I-1: 同一 attacker 跨入口互斥人格
    conflicts = {tuple(sorted(p)) for p in cfg["role_conflicts"]}
    by_attacker: dict[str, set] = {}
    for r in pack.roles:
        by_attacker.setdefault(r.attacker_id, set()).add(r.role)
    for roles in by_attacker.values():
        for pair in conflicts:
            if pair[0] in roles and pair[1] in roles:
                counts["I-1"] += 1

    # 加权汇总（整数运算）：w_c = 0.25 → 分子 Σcounts，分母 4×interactions
    interactions = (len(pack.auth) + len(pack.data) + len(pack.roles)
                    + len(pack.reach))
    total = sum(counts.values())
    den = 4 * max(1, interactions)
    return {
        "config_version": cfg["version"],
        "interactions": interactions,
        "entries": entries,
        "rules": counts,
        "contradictions_total": total,
        "csvr_num": total,
        "csvr_den": den,
        "csvr_per_mille": total * 1000 // den,
        "recall_interval": None,
        "valid": False,
        "note": ("单入口视图：跨入口规则（T-1/T-2/C-1/C-2/M-2/I-1）恒 0，"
                 "接入第二入口后才有意义" if single else
                 "recall 区间缺失（12 §4.3）：M2 标注回填前该点值不作为结论引用"),
    }
