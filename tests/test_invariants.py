"""四条不变量：检测器必须对注入的坏状态报警（12 §3；先断言后代码的"断言"侧）。"""
from __future__ import annotations

import pytest

from dws.facts import Fact
from dws.ids import entity_uuid
from dws.invariants import check_all, check_i1, check_i2, check_i3, check_i4
from dws.world import CommitRejected, World
from tests.fixtures.gen_demo_fixtures import build_seed

LATER = "1792166500.0000/replay"    # 晚于种子 now（BASE=1792166400）
EARLIER = "1792166300.0000/replay"  # 早于种子 now


@pytest.fixture()
def world(demo_seed):
    return World(demo_seed)


def test_legal_world_zero_violations(world):
    assert check_all(world) == []


# ------------------------------------------------ I1 时间线单调

def test_i1_commit_rejects_timeline_regression(world):
    p = {"event_id_ref": "evt-1", "object_ref": None, "kind": "login"}
    world.commit(None, [("timeline", p)], 1, LATER, attacker_id="atk-a")
    with pytest.raises(CommitRejected):
        world.commit(None,
                     [("timeline", dict(p, event_id_ref="evt-2"))],
                     world.meta["world_version"], EARLIER, attacker_id="atk-a")


def test_i1_detects_mtime_before_ctime(world):
    eid = entity_uuid("data", "/home/deploy/notes.txt")
    world.facts["data"][eid][-1].payload["mtime_hlc"] = EARLIER
    assert any(v.rule == "I1" for v in check_i1(world))


def test_i1_detects_mtime_after_now(world):
    eid = entity_uuid("data", "/home/deploy/notes.txt")
    world.facts["data"][eid][-1].payload["mtime_hlc"] = "9999999999.0000/replay"
    assert any(v.rule == "I1" for v in check_i1(world))


# ------------------------------------------------ I2 凭据单一有效

def _deploy_secret() -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(b"deploy-password").hexdigest()[:40]


def _narrowed_payload():
    return {"secret_hash": _deploy_secret(),
            "owner_entity_id": entity_uuid("identity", "deploy"),
            "valid_on": [],
            "revoked_at_hlc": None}


def test_i2_commit_rejects_silent_scope_narrowing(world):
    """同一凭据实体 v2 收窄作用域且无吊销事件 → 提交被拒（契约 §3 I2 v1.1）。"""
    with pytest.raises(CommitRejected):
        world.commit(None, [("credential", _narrowed_payload())], 1, LATER)


def test_i2_allows_scope_change_with_revocation_event(world):
    """先落显式吊销事件，再收窄作用域 → 合法提交。"""
    owner = entity_uuid("identity", "deploy")
    cred_eid = entity_uuid("credential", f"{owner}:{_deploy_secret()}")
    rev = {"event_id_ref": "rev-1", "object_ref": f"credential:{cred_eid}",
           "kind": "revocation"}
    world.commit(None, [("timeline", rev)], 1, LATER, attacker_id="atk-x")
    payload = _narrowed_payload()
    payload["revoked_at_hlc"] = LATER
    r = world.commit(None, [("credential", payload)],
                     world.meta["world_version"], LATER)
    assert r.ok and r.world_version == 3


def test_i2_detects_silent_narrowing(world):
    """直接注入 v2（绕过提交校验）→ check_i2 必须报警。"""
    f = Fact.new("credential", _narrowed_payload(), version=2, hlc=LATER,
                 node_id="replay", attacker_id=None, world_version=2)
    world.facts["credential"][f.entity_id].append(f)
    assert any(v.rule == "I2" for v in check_i2(world))


# ------------------------------------------------ I3 可达性闭包

def test_i3_commit_rejects_undeclared_port(world):
    rel = {"from_entity_id": entity_uuid("host", "web01"),
           "to_entity_id": entity_uuid("host", "db01"),
           "kind": "l3-reachable", "ports": [9999]}
    with pytest.raises(CommitRejected):
        world.commit(None, [("relation", rel)], 1, LATER)


def test_i3_detects_undeclared_port(world):
    eid = entity_uuid("relation",
                      f"{entity_uuid('host', 'web01')}->{entity_uuid('host', 'db01')}:l3-reachable")
    world.facts["relation"][eid][-1].payload["ports"] = [9999]
    assert any(v.rule == "I3" for v in check_i3(world))


# ------------------------------------------------ I4 版本单调

def test_i4_detects_version_gap(world):
    eid = entity_uuid("host", "web01")
    world.facts["host"][eid][0].version = 7
    assert any(v.rule == "I4" for v in check_i4(world))


def test_i4_detects_global_regression(world):
    world.meta["world_version"] = 0
    assert any(v.rule == "I4" for v in check_i4(world))


def test_commit_rollback_is_clean(world):
    """被拒绝的提交必须零残留（回滚干净）。"""
    before = {cls: {e: len(rows) for e, rows in ents.items()}
              for cls, ents in world.facts.items()}
    rel = {"from_entity_id": entity_uuid("host", "web01"),
           "to_entity_id": entity_uuid("host", "db01"),
           "kind": "l3-reachable", "ports": [9999]}
    with pytest.raises(CommitRejected):
        world.commit(None, [("relation", rel)], 1, LATER)
    after = {cls: {e: len(rows) for e, rows in ents.items()}
             for cls, ents in world.facts.items()}
    assert before == after
    assert world.meta["world_version"] == 1
