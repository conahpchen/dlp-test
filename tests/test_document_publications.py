import time
from threading import Event

from fastapi.testclient import TestClient

from business import app as visitor
from business import atomic_state
from business.atomic_state import read
from dws.ids import entity_uuid
from management.app import app as dashboard
from research.api import current_lab


def test_approved_document_publishes_and_rolls_back_through_writer(monkeypatch, tmp_path):
    checkpoint = tmp_path / "private" / "world.sqlite"
    projection = tmp_path / "ssh" / "world.sqlite"
    monkeypatch.setenv("HONEYNET_DWS_ATOMIC", "1")
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(checkpoint))
    monkeypatch.setenv("HONEYNET_COWRIE_EXPORT", str(projection))
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-operator")
    monkeypatch.setattr(visitor, "_engine", None)
    monkeypatch.setattr(visitor, "_audit_rows", [])
    monkeypatch.setattr(visitor, "_events", [])
    monkeypatch.setattr(visitor, "_event_ids", set())
    operator = TestClient(dashboard)
    auth = ("operator", "synthetic-operator")
    with TestClient(visitor.app) as public:
        assert public.get("/api/publications").status_code == 404
        engine = visitor.get_engine()
        path = "/kb/managed.md"
        fact_id = entity_uuid("data", path)
        old_version = engine.world.meta["world_version"]
        lab = current_lab()
        old_run = lab.create(engine.world, "plain")["run_id"]
        body = dict(path=path, content="Synthetic published document", base_version=old_version)
        assert operator.get("/publications", auth=auth).status_code == 200
        assert operator.post("/api/publications", json=body).status_code == 401
        initial = operator.get("/api/publication-documents", auth=auth).json()
        assert initial["world_version"] == old_version
        assert any(item["path"] == "/kb/handbook.md" for item in initial["documents"])
        draft = operator.post("/api/publications", auth=auth, json=body).json()
        assert draft["status"] == "draft"
        assert path not in read(projection)["files"]
        url = f"/api/publications/{draft['id']}"
        original_store = atomic_state._store
        failed = Event()

        def fail_ssh_write(db, schema, payload):
            original_store(db, schema, payload)
            if schema == "ssh":
                failed.set()
                raise OSError("injected publication failure")

        with monkeypatch.context() as patch:
            patch.setattr(atomic_state, "_store", fail_ssh_write)
            assert operator.post(url + "/approve", auth=auth, json={}).json()["status"] == "approved"
            assert failed.wait(timeout=5)
            with visitor._lock:
                assert engine.world.meta["world_version"] == old_version
            assert read(projection)["world_version"] == old_version
            assert read(checkpoint)["meta"]["world_version"] == old_version

        def wait_for(status, identifier=draft["id"]):
            end = time.monotonic() + 6
            while time.monotonic() < end:
                rows = operator.get("/api/publications", auth=auth).json()
                row = next(row for row in rows if row["id"] == identifier)
                if row["status"] == status:
                    return row
                time.sleep(.1)
            raise AssertionError(row)

        published = wait_for("active")
        active_version = published["result_version"]
        assert engine.world.view(old_version).current("data", fact_id) is None
        assert engine.world.view(active_version).current("data", fact_id).payload["content"] == body["content"]
        assert read(projection)["files"][path]["content"] == body["content"]
        assert read(checkpoint)["meta"]["world_version"] == read(projection)["world_version"] == active_version
        diff = operator.get("/api/world", params={"kind": "data", "version": active_version,
                                                  "baseline": old_version}, auth=auth).json()
        item = next(row for row in diff["diff"] if row["after"]["label"] == path)
        assert item["change"] == "added" and item["before"] is None
        assert "content" not in item["after"]
        fresh_run = lab.create(engine.world, "plain")["run_id"]
        old_documents = public.get(f"/decoy/{old_run}/index").json()["documents"]
        fresh_documents = public.get(f"/decoy/{fresh_run}/index").json()["documents"]
        assert not any(row["path"] == path for row in old_documents)
        fresh_document = next(row for row in fresh_documents if row["path"] == path)
        assert public.get(fresh_document["href"]).json()["content"] == body["content"]

        stale = operator.post("/api/publications", auth=auth,
                              json=dict(body, content="Conflicting replacement")).json()
        assert operator.post(f"/api/publications/{stale['id']}/approve", auth=auth, json={}).status_code == 200
        assert "changed" in wait_for("rejected", stale["id"])["error"]

        assert operator.post(url + "/rollback", auth=auth, json={}).json()["status"] == "rollback_pending"
        rolled_back = wait_for("rolled_back")
        tombstone = engine.world.view(rolled_back["result_version"]).current("data", fact_id)
        assert tombstone.payload["deleted"]
        assert engine.world.view(active_version).current("data", fact_id).payload["content"] == body["content"]
        assert read(projection)["files"][path]["deleted"]


def test_retrying_document_does_not_hold_later_approved_jobs(monkeypatch, tmp_path):
    from business import publication_retry, publications

    checkpoint = tmp_path / "world.sqlite"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(checkpoint))
    checkpoint.touch()
    monkeypatch.setattr(publication_retry, "time", lambda: 2_000_000_000)
    base = {"content": "Synthetic queue test", "base_version": 1}
    first, second = [publications.submit(publications.DocumentProposal(path=f"/kb/{name}.md", **base))["id"]
                     for name in ("first", "second")]
    for identifier in (first, second):
        publications.request_transition(identifier, "approve")
    attempted = []

    def apply(engine, row, checkpoint):
        attempted.append(row["id"])
        if row["id"] == first:
            raise OSError("simulated persistent failure")
        return 2

    monkeypatch.setattr(publications, "_apply", apply)
    publications.apply_next(None, None)
    publications.apply_next(None, None)
    assert attempted == [first, second]
    for _ in range(publication_retry.MAX_ATTEMPTS - 1):
        with publications.queue() as db:
            db.execute("UPDATE publication_retry SET retry_at=0 WHERE lane='document' AND job=?", (first,))
        publications.apply_next(None, None)
    proposals = {row["id"]: row for row in publications.list_proposals()}
    assert proposals[first]["status"] == "failed" and proposals[first]["error"] == "OSError"
    assert proposals[second]["status"] == "active" and proposals[second]["result_version"] == 2
