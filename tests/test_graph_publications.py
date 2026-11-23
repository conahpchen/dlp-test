import time
from threading import Event

from fastapi.testclient import TestClient

from business import app as visitor, atomic_state, graph_publications
from management.app import app as dashboard
from research.api import current_lab
from tests.test_generative_research import SceneModel


def test_graph_approval_rollback_and_public_snapshots(monkeypatch, tmp_path):
    checkpoint = tmp_path / "private" / "world.sqlite"
    projection = tmp_path / "ssh" / "world.sqlite"
    monkeypatch.setenv("HONEYNET_DWS_ATOMIC", "1")
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(checkpoint))
    monkeypatch.setenv("HONEYNET_COWRIE_EXPORT", str(projection))
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-operator")
    for name, value in [("_engine", None), ("_audit_rows", []), ("_events", []), ("_event_ids", set())]:
        monkeypatch.setattr(visitor, name, value)
    management = TestClient(dashboard)
    auth = ("operator", "synthetic-operator")
    endpoint = "/api/graph-publications"

    def wait_for(identifier, status):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            row = next(item for item in management.get(endpoint, auth=auth).json()
                       if item["id"] == identifier)
            if row["status"] == status:
                return row
            time.sleep(.1)
        raise AssertionError(row)

    with TestClient(visitor.app) as public:
        engine = visitor.get_engine()
        base = engine.world.meta["world_version"]
        lab = current_lab()
        old_run = lab.create(engine.world, "plain", model=SceneModel())["run_id"]
        body = dict(
            base_version=base, scope="public",
            nodes=[dict(id="availability", kind="document", content="Synthetic availability guide"),
                   dict(id="reporting", kind="database")],
            edges=[dict(source="availability", target="reporting")],
        )
        assert management.post(endpoint, json=body).status_code == 401
        assert public.post(endpoint, json=body).status_code == 404
        dangling = dict(body, edges=[dict(source="availability", target="missing")])
        assert management.post(endpoint, auth=auth, json=dangling).status_code == 422
        many_targets = dict(body, nodes=body["nodes"] + [dict(id="one", kind="database"),
                                                         dict(id="two", kind="database")],
                            edges=[dict(source="availability", target=target)
                                   for target in ("reporting", "one", "two")])
        assert management.post(endpoint, auth=auth, json=many_targets).status_code == 422
        result = management.post(endpoint, auth=auth, json=body)
        assert result.status_code == 201, result.text
        identifier = result.json()["id"]
        url = f"{endpoint}/{identifier}"
        failed = Event()
        original_store = atomic_state._store

        def fail_terminal_projection(db, schema, payload):
            original_store(db, schema, payload)
            if schema == "ssh":
                failed.set()
                raise OSError("synthetic graph checkpoint failure")

        with monkeypatch.context() as patch:
            patch.setattr(atomic_state, "_store", fail_terminal_projection)
            assert management.post(url + "/approve", auth=auth, json={}).json()["status"] == "approved"
            assert failed.wait(timeout=5)
            with visitor._lock:
                assert engine.world.meta["world_version"] == base
            assert atomic_state.read(checkpoint)["meta"]["world_version"] == base
            assert atomic_state.read(projection)["world_version"] == base

        at_release = wait_for(identifier, "active")["result_version"]
        current_run = lab.create(engine.world, "plain", model=SceneModel())["run_id"]
        old_path, current_path = (f"/decoy/{run}/world-graph" for run in (old_run, current_run))
        assert public.get(old_path).json() == {"graphs": []}
        graph, = public.get(current_path).json()["graphs"]
        assert graph["version"] == at_release
        assert graph["edges"] == body["edges"]
        path = "/kb/graph-availability.md"
        assert atomic_state.read(projection)["files"][path]["content"] == body["nodes"][0]["content"]
        assert management.post(url + "/withdraw", auth=auth, json={}).status_code == 200
        withdrawn = wait_for(identifier, "withdrawn")["result_version"]
        assert withdrawn > at_release
        assert public.get(current_path).json() == {"graphs": [graph]}
        after_run = lab.create(engine.world, "plain", model=SceneModel())["run_id"]
        assert public.get(f"/decoy/{after_run}/world-graph").json() == {"graphs": []}
        assert atomic_state.read(projection)["files"][path]["deleted"]

        branch_body = dict(body, scope="branch", base_version=withdrawn,
                           nodes=[dict(id="session_guide", kind="document", content="Private synthetic guide"),
                                  dict(id="session_index", kind="database")],
                           edges=[dict(source="session_guide", target="session_index")])
        lab.activate(after_run)
        bindings = f"/api/evidence/runs/{after_run}/bindings"
        result = management.post(endpoint, auth=auth, json=branch_body)
        assert result.status_code == 201
        branch = result.json()["id"]
        branch_url = f"{endpoint}/{branch}"
        committed = Event()
        publish = graph_publications._publish

        def interrupt_after_commit(*args):
            version = publish(*args)
            committed.set()
            raise OSError("synthetic writer interrupted before its queue update")

        with monkeypatch.context() as patch:
            patch.setattr(graph_publications, "_publish", interrupt_after_commit)
            assert management.post(branch_url + "/approve", auth=auth, json={}).status_code == 200
            assert committed.wait(timeout=5)
            branch_version = atomic_state.read(checkpoint)["meta"]["world_version"]
            assert wait_for(branch, "approved")["result_version"] is None
            # Reopen the writer from its committed checkpoint before retrying the queue.
            with visitor._lock:
                visitor._engine = None
        assert wait_for(branch, "active")["result_version"] == branch_version
        assert atomic_state.read(checkpoint)["meta"]["world_version"] == branch_version
        grant = management.post(bindings, params={"publication": branch, "target": "web"}, auth=auth, json={})
        assert grant.status_code == 201
        branch_headers = {"X-Honeynet-Binding": grant.json()["binding"]}
        branch_path = f"/decoy/{after_run}/world-graph"
        assert public.get(branch_path, headers=branch_headers).json()["graphs"][0]["version"] == branch_version
        assert management.post(branch_url + "/withdraw", auth=auth, json={}).status_code == 200
        wait_for(branch, "withdrawn")
        assert management.post(bindings, params={"publication": branch, "target": "web"},
                               auth=auth, json={}).status_code == 409
        assert public.get(branch_path).json() == {"graphs": []}
        assert public.get(branch_path, headers=branch_headers).json()["graphs"][0]["version"] == branch_version
