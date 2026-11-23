"""Approvals survive restarts and require the same evidence revision."""

from dws.world import World
from research.lab import ResearchLab
from research.responses import decide, list_requests, submit


def test_response_rejection_and_conflict_preserve_evidence(demo_seed, tmp_path):
    filename = tmp_path / "research.sqlite"
    lab = ResearchLab(filename)
    run = lab.create(World(demo_seed), "agent_note")["run_id"]
    result = lab.visit(run, "documents/0", "source", "127.0.0.1", "browser")[1]
    token = result["verification_link"].rsplit("/", 1)[1]
    lab.record_lure_response(run, "source", token)
    assert lab.observe_lure_use(token, "ssh", "target")
    first = lab.canary_uses(run)[0]
    identifier = first["use_id"]
    pending = submit(lab, run, "detach", str(identifier), "Review connection overlap")
    restarted = ResearchLab(filename)
    assert list_requests(restarted)[0] == pending
    assert decide(restarted, pending["id"], False, "Not sufficient")["status"] == "rejected"
    assert restarted.canary_uses(run)[0]["associated"]
    stale = submit(restarted, run, "detach", str(identifier), "Review again")
    restarted.revise_canary_use(identifier, 0, False, "Separate prior review")
    restarted.revise_canary_use(identifier, 1, True, "Preserve exact link")
    assert decide(restarted, stale["id"], True, "As submitted")["status"] == "conflict"
    assert restarted.canary_uses(run)[0]["revision"] == 2
    detach = submit(restarted, run, "detach", str(identifier), "Review new revision")
    assert decide(restarted, detach["id"], True, "Confirmed")["status"] == "applied"
    restore = submit(restarted, run, "restore", str(identifier), "Corroborated")
    assert decide(restarted, restore["id"], True, "Confirmed")["status"] == "applied"
    assert restarted.canary_uses(run)[0]["associated"]
    assert restarted.canary_uses(run)[0]["revision"] == 4
