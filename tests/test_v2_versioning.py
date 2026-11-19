import copy

from fastapi.testclient import TestClient

from business import storage
from dws.world import World
from entries.gateway import app as gateway
from replay.engine import Engine
from research.lab import ResearchLab


def publish(engine):
    world = engine.world
    host = next(f for f in world.all_current_cls("host") if f.payload["hostname"] == "ai01")
    payload = copy.deepcopy(host.payload)
    next(s for s in payload["services"] if s["proto"] == "openai")["ver"] = "corp-next"
    engine.commit_external(None, [("host", payload)])


def test_fake_api_listing_and_chat_keep_bound_version(demo_seed, monkeypatch):
    engine = Engine(World(demo_seed))
    monkeypatch.setattr(gateway, "_engine", engine)
    client = TestClient(gateway.app)
    headers = {"Authorization": "Bearer sk-demo-internal-2026", "X-Session": "prior"}
    assert client.get("/v1/models", headers=headers).json()["data"][0]["id"] == "corp-llm-7b"
    publish(engine)
    assert client.get("/v1/models", headers=headers).json()["data"][0]["id"] == "corp-llm-7b"
    response = client.post("/v1/chat/completions", headers=headers, json={"model": "corp-llm-7b"})
    assert response.status_code == 200
    assert "x-deception" not in response.headers
    assert client.get("/v1/models").json()["data"][0]["id"] == "corp-next"


def test_checkpoint_restores_pins_and_private_writes(demo_seed, monkeypatch, tmp_path):
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(tmp_path / "world.json"))
    engine = Engine(World(demo_seed))
    world = engine.world
    session = world.open_session("old", "fixture", "deploy", None)
    fact = world.all_current_cls("data")[0]
    engine.commit_external("old", [("data", dict(fact.payload, content="own write"))])
    publish(engine)
    recovered = Engine(World(demo_seed))
    storage.persist(engine, [], [])
    storage.restore(recovered)
    restored = recovered.world.sessions["old"]
    assert restored.pinned_version == session.pinned_version
    view = recovered.world.view(restored.pinned_version, restored.own_versions)
    assert view.current("data", fact.entity_id).payload["content"] == "own write"
    assert view.all_current_cls("host") != recovered.world.all_current_cls("host")


def test_web_cookie_keeps_run_after_activation_and_restart(tmp_path):
    lab = ResearchLab(tmp_path / "research.db")
    assert lab.active_for("old") is None
    with lab.connect() as db:
        db.execute("INSERT INTO settings VALUES ('active_run', 'first')")
    assert lab.active_for("old") == "first"
    with lab.connect() as db:
        db.execute("UPDATE settings SET value='second' WHERE key='active_run'")
    recovered = ResearchLab(lab.path)
    assert recovered.active_for("old") == "first"
    assert recovered.active_for("new") == "second"


def test_ssh_connection_pins_auth_and_ls_cat_after_publish(demo_seed, demo_events):
    engine = Engine(World(demo_seed))
    events = copy.deepcopy(demo_events)
    connected = engine.process(events[0])
    fact = next(f for f in engine.world.all_current_cls("data")
                if f.payload["path"] == "/home/deploy/notes.txt")
    engine.commit_external(None, [("data", dict(fact.payload, content="new publication", deleted=True))])
    responses = [engine.process(event) for event in events[1:10]]
    assert all(response["dws_version"] == connected["dws_version"] for response in responses)
    assert "notes.txt" in "\n".join(responses[-2]["output"])
    assert "rotate ssh keys" in "\n".join(responses[-1]["output"])


def test_web_lure_is_stable_unique_and_scoped_to_session(demo_seed, tmp_path):
    lab = ResearchLab(tmp_path / "research.sqlite")
    run = lab.create(World(demo_seed), "agent_note")["run_id"]

    def link(current, session):
        return current.visit(run, "documents/0", session, "127.0.0.1", "browser")[1]["verification_link"]

    first = link(lab, "first")
    recovered = ResearchLab(lab.path)
    assert first == link(recovered, "first")
    assert first != link(recovered, "second")
    resource = first.split(f"/decoy/{run}/")[1]
    assert recovered.visit(run, resource, "second", "127.0.0.1", "browser")[0] == 404
    assert recovered.visit(run, resource, "first", "127.0.0.1", "browser")[0] == 200


def test_failed_web_activation_keeps_previous_release_after_restart(demo_seed, tmp_path):
    import json

    import pytest
    from tests.test_generative_research import SceneModel

    lab = ResearchLab(tmp_path / "research.sqlite")
    world = World(demo_seed)
    first = lab.create(world, "plain", model=SceneModel())["run_id"]
    second = lab.create(world, "plain", model=SceneModel())["run_id"]
    lab.activate(first)
    plan = lab.plan(second)
    plan["scene"]["entry_node"] = "missing"
    with lab.connect() as db:
        db.execute("UPDATE runs SET plan=? WHERE id=?", (json.dumps(plan), second))
    with pytest.raises(ValueError):
        lab.activate(second)
    recovered = ResearchLab(lab.path)
    assert recovered.active() == first
    assert recovered.rollback(first) == {"active_run": None}
    assert ResearchLab(lab.path).active() is None


def test_ssh_simulated_write_stays_private_after_restart(demo_seed, demo_events, monkeypatch, tmp_path):
    from dws.ids import entity_uuid

    monkeypatch.setenv("BUSINESS_STATE_FILE", str(tmp_path / "world.json"))
    engine = Engine(World(demo_seed))
    events = copy.deepcopy(demo_events)
    for event in events[:11]:
        engine.process(event)
    path = entity_uuid("data", "/home/deploy/report.txt")
    session_id = events[0]["session"]
    assert engine.session_view(session_id).current("data", path) is not None
    assert engine.session_view().current("data", path) is None
    storage.persist(engine, [], [])
    recovered = Engine(World(demo_seed))
    storage.restore(recovered)
    assert recovered.session_view(session_id).current("data", path) is not None
    fresh = recovered.world.open_session("fresh", "other", "deploy", None)
    assert recovered.session_view(fresh.session_id).current("data", path) is None


def test_revoked_and_expired_canaries_cannot_create_new_evidence(demo_seed, tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setattr("research.lab.time", lambda: 1000)
    lab = ResearchLab(tmp_path / "research.sqlite")
    run = lab.create(World(demo_seed), "agent_note")["run_id"]

    def issue(session):
        link = lab.visit(run, "documents/0", session, "127.0.0.1", "browser")[1]["verification_link"]
        return link.rsplit("/", 1)[-1]

    revoked = issue("first")
    assert not lab.observe_lure_use(revoked, "llm", "unissued-use")
    lab.record_lure_response(run, "first", revoked)
    assert lab.observe_lure_use(revoked, "llm", "before-revocation")
    identifier = hashlib.sha256(revoked.encode()).hexdigest()
    assert lab.revoke_canary(identifier)["status"] == "revoked"
    assert not lab.observe_lure_use(revoked, "llm", "after-revocation")
    assert lab.visit(run, "beacon/" + revoked, "first", "127.0.0.1", "browser")[0] == 404
    assert "verification_link" not in lab.visit(run, "documents/0", "first", "127.0.0.1", "browser")[1]

    expired = issue("second")
    lab.record_lure_response(run, "second", expired)
    monkeypatch.setattr("research.lab.time", lambda: 1000 + 24 * 60 * 60)
    assert not lab.observe_lure_use(expired, "llm", "after-expiry")
    assert lab.visit(run, "beacon/" + expired, "second", "127.0.0.1", "browser")[0] == 404
    with lab.connect() as db:
        assert db.execute("SELECT session FROM lure_uses").fetchall() == [("before-revocation",)]


def test_cowrie_bridge_correlates_a_delivered_ssh_password_without_exporting_it(demo_seed, tmp_path):
    import json

    from entries.cowrie.bridge import main

    lab = ResearchLab(tmp_path / "research.sqlite")
    run = lab.create(World(demo_seed), "agent_note")["run_id"]
    link = lab.visit(run, "documents/0", "web", "127.0.0.1", "browser")[1]["verification_link"]
    token = link.rsplit("/", 1)[-1]
    lab.record_lure_response(run, "web", token)
    source, output = tmp_path / "cowrie.json", tmp_path / "export.jsonl"
    source.write_text(json.dumps({
        "eventid": "cowrie.login.failed", "session": "test-ssh", "username": "catalog",
        "password": token, "src_ip": "127.0.0.1",
    }) + "\n")
    args = ["--in", str(source), "--out", str(output), "--canary-state", str(lab.path)]
    assert main(args) == 0
    assert main(args) == 0
    assert token not in output.read_text()
    credential = lab.canaries(run)[0]
    with lab.connect() as db:
        assert db.execute("SELECT token_hash,entry,session FROM lure_uses").fetchall() == [
            (credential["credential_id"], "ssh", "sensor-a/test-ssh"),
        ]


def test_canary_association_revisions_preserve_original_observation(demo_seed, tmp_path):
    import pytest

    lab = ResearchLab(tmp_path / "research.sqlite")
    run = lab.create(World(demo_seed), "agent_note")["run_id"]
    link = lab.visit(run, "documents/0", "web", "127.0.0.1", "browser")[1]["verification_link"]
    token = link.rsplit("/", 1)[-1]
    lab.record_lure_response(run, "web", token)
    assert lab.observe_lure_use(token, "llm", "session")
    (initial,) = lab.canary_uses(run)
    use_id = initial["use_id"]
    revised = lab.revise_canary_use(use_id, 0, False, "Shared credential")
    assert not revised["associated"]
    with pytest.raises(ValueError):
        lab.revise_canary_use(use_id, 0, True, "Concurrent update")
    recovered = ResearchLab(lab.path)
    assert recovered.canary_uses(run) == [revised]
    restored = recovered.revise_canary_use(use_id, 1, True, "Verified provenance")
    assert restored["associated"]
    assert restored["recorded_at"] == initial["recorded_at"]
    assert [revision["associated"] for revision in restored["history"]] == [False, True]
