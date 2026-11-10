"""CSVR v0 规则（契约 12 §4.2）与申报合规（§4.3）。"""
from __future__ import annotations

import copy

from dws.world import World
from scorer.csvr import EvidencePack, AuthOutcome, DataObservation, ReachObservation, \
    RoleGrant, score
from scorer.report import render


def _pack(demo_seed):
    return EvidencePack(worlds={"a": World(demo_seed)})


def test_single_entry_coherent_zero(demo_seed):
    pack = _pack(demo_seed)
    now = pack.worlds["a"].meta["now_hlc"]
    pack.auth.append(AuthOutcome("a", "sha256:x", "web01", True))
    pack.data.append(DataObservation("a", "/home/deploy/notes.txt", now, now))
    pack.roles.append(RoleGrant("a", "atk-1", "sysadmin"))
    pack.reach.append(ReachObservation("a", "web01", "db01", True))
    result = score(pack)
    assert result["contradictions_total"] == 0
    assert result["csvr_per_mille"] == 0
    assert result["valid"] is False and result["recall_interval"] is None


def test_c1_same_secret_success_and_failure(demo_seed):
    pack = EvidencePack(worlds={"a": World(demo_seed), "b": World(demo_seed)})
    pack.auth.append(AuthOutcome("a", "sha256:shared", "web01", True))
    pack.auth.append(AuthOutcome("b", "sha256:shared", "web01", False))
    result = score(pack)
    assert result["rules"]["C-1"] == 1


def test_c2_revocation_view_divergence(demo_seed):
    world_a = World(demo_seed)
    world_b = World(demo_seed)
    for f in world_b.all_current_cls("credential"):
        f.payload["revoked_at_hlc"] = world_b.meta["now_hlc"]
    pack = EvidencePack(worlds={"a": world_a, "b": world_b})
    result = score(pack)
    assert result["rules"]["C-2"] >= 1


def test_m1_mtime_out_of_bounds(demo_seed):
    pack = _pack(demo_seed)
    pack.data.append(DataObservation("a", "/x", "1792166400.0000/replay",
                                     "1792166300.0000/replay"))  # mtime < ctime
    assert score(pack)["rules"]["M-1"] == 1


def test_i1_role_conflict(demo_seed):
    pack = EvidencePack(worlds={"a": World(demo_seed), "b": World(demo_seed)})
    pack.roles.append(RoleGrant("a", "atk-9", "intern"))
    pack.roles.append(RoleGrant("b", "atk-9", "dba"))
    assert score(pack)["rules"]["I-1"] == 1


def test_t2_host_exists_only_in_b(demo_seed):
    world_a = World(demo_seed)
    seed_b = copy.deepcopy(demo_seed)
    seed_b["initial_facts"]["host"].append(
        {"hostname": "ghost01", "ip": "10.20.3.99", "os": "Ubuntu 22.04.3 LTS",
         "services": [{"port": 22, "proto": "ssh", "ver": "OpenSSH 9.3p1", "banner": ""}],
         "banner": None})
    world_b = World(seed_b)
    pack = EvidencePack(worlds={"a": world_a, "b": world_b})
    pack.reach.append(ReachObservation("a", "web01", "ghost01", False))
    assert score(pack)["rules"]["T-2"] == 1


def test_report_marks_point_value_invalid(demo_seed):
    pack = _pack(demo_seed)
    report = render(pack)
    assert report["compliance"].startswith("INVALID_FOR_PUBLICATION")
    assert report["formula"].startswith("CSVR =")
