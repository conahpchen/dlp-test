import os

import pytest

from business import atomic_state, storage
from dws.world import World
from replay.engine import Engine


def test_publication_rollback_and_recovery(demo_seed, tmp_path, monkeypatch):
    checkpoint = tmp_path / "private" / "world.sqlite"
    projection = tmp_path / "ssh" / "world.sqlite"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(checkpoint))
    monkeypatch.setenv("HONEYNET_COWRIE_EXPORT", str(projection))
    engine = Engine(World(demo_seed))
    storage.persist(engine, [], [])
    before = engine.world.meta["world_version"]
    note = next(f for f in engine.world.all_current_cls("data")
                if f.payload["path"] == "/home/deploy/notes.txt")
    engine.commit_external(None, [("data", dict(note.payload, content="published"))])

    store = atomic_state._store

    def fail_after_writes(db, schema, payload):
        store(db, schema, payload)
        if schema == "ssh":
            raise OSError("injected projection failure")

    with monkeypatch.context() as patch:
        patch.setattr(atomic_state, "_store", fail_after_writes)
        with pytest.raises(OSError, match="injected projection failure"):
            storage.persist(engine, [], [])
    recovered = Engine(World(demo_seed))
    storage.restore(recovered)
    assert recovered.world.meta["world_version"] == before
    assert atomic_state.read(projection)["world_version"] == before

    storage.persist(engine, [], [])
    storage.restore(recovered)
    assert recovered.world.meta["world_version"] == before + 1
    public = atomic_state.read(projection)
    assert public["world_version"] == before + 1
    assert public["files"]["/home/deploy/notes.txt"]["content"] == "published"
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    assert projection.stat().st_mode & 0o777 == 0o644


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires process exit fault injection")
def test_uncommitted_dual_write_remains_invisible_after_process_exit(tmp_path):
    checkpoint = tmp_path / "state.sqlite"
    projection = tmp_path / "ssh" / "world.sqlite"
    atomic_state.commit(checkpoint, {"version": 1}, projection, {"version": 1})
    original = atomic_state._store
    pid = os.fork()
    if pid == 0:
        def exit_after_projection(db, schema, payload):
            original(db, schema, payload)
            if schema == "ssh":
                os._exit(42)

        atomic_state._store = exit_after_projection
        atomic_state.commit(checkpoint, {"version": 2}, projection, {"version": 2})
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 42
    assert atomic_state.read(checkpoint) == {"version": 1}
    assert atomic_state.read(projection) == {"version": 1}
    atomic_state.commit(checkpoint, {"version": 3}, projection, {"version": 3})
    assert atomic_state.read(checkpoint) == atomic_state.read(projection) == {"version": 3}


def test_fake_api_rolls_back_live_event_when_publication_fails(demo_seed, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from business import app as business

    checkpoint = tmp_path / "private" / "world.sqlite"
    projection = tmp_path / "ssh" / "world.sqlite"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(checkpoint))
    monkeypatch.setenv("HONEYNET_COWRIE_EXPORT", str(projection))
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setattr(business, "_engine", None)
    monkeypatch.setattr(business, "_events", [])
    monkeypatch.setattr(business, "_event_ids", set())
    engine = business.get_engine()
    before = engine.world.meta["world_version"]
    writer = atomic_state._store

    def fail(db, schema, payload):
        writer(db, schema, payload)
        if schema == "ssh":
            raise OSError("Injected error")

    client = TestClient(business.app, raise_server_exceptions=False)
    arguments = dict(json={"model": "corp-llm-7b"},
                     headers={"Authorization": "Bearer sk-demo-internal-2026"})
    with monkeypatch.context() as patch:
        patch.setattr(atomic_state, "_store", fail)
        response = client.post("/v1/chat/completions", **arguments)
    assert response.status_code == 500
    assert engine.world.meta["world_version"] == before
    assert not business._events
    assert client.post("/v1/chat/completions", **arguments).status_code == 200
    assert engine.world.meta["world_version"] == before + 1
    assert atomic_state.read(projection)["world_version"] == before + 1
