import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from contextlib import closing
import sqlite3
from threading import Event

import httpx
import pymysql
import pytest
from fastapi.testclient import TestClient

from business import app as visitor, atomic_state
from deploy.state_backup import restore as restore_bundle, snapshot as snapshot_bundle
from research import protocol_decoys
from research.api import current_lab
from research.controller import adapt
from research import artifacts
from research.generation import MAX_SCENE_DEGREE, scene_edges, validate_scene
from research.grounding import protocol_graph
from research.model_input import model_event
from research.service_planner import isolated_runtime
from tests.test_generative_research import SceneModel
from tests.test_v2_loopback import listener


class GroundedModel(SceneModel):
    def choose(self, messages):
        context = json.loads(messages[-1]["content"])
        result = super().choose(messages)
        if "honeynet builder AI" in messages[0]["content"]:
            entities = context["entity_graph"]["entities"]
            relation = next(edge for edge in context["entity_graph"]["relations"] if edge["relation"] == "hosted_on")
            assert {relation["source"], relation["target"]} <= {item["id"] for item in entities}
            result["entity_links"] = [
                {"node_id": "overview", "entity_id": relation[key], "relation": "reviews"}
                for key in ("source", "target")
            ]
            result["nodes"][0]["sources"] = []
            result["nodes"][1]["sources"] = []
            result["services"] = [{"id": "catalog-check", "method": "GET", "path": "catalog/check",
                                   "node_id": "evidence", "data": {"status": "ready"}}]
        elif "scene_patch" in result:
            result["scene_patch"]["services"] = [{"id": "change-check", "method": "GET", "path": "change/check",
                                                   "node_id": "evidence", "data": {"status": "reviewed"}}]
        return result


class GroundedProtocolModel:
    def choose(self, messages):
        context = json.loads(messages[-1]["content"])
        return {"intent": "documentation", "title": context["protocol"] + "_review",
                "note": "Synthetic catalog inspection reference.",
                "entity_id": context["entity_graph"]["entities"][0]["id"]}


def setup_environment(monkeypatch, tmp_path):
    checkpoint = tmp_path / "private" / "world.sqlite"
    projection = tmp_path / "ssh" / "world.sqlite"
    for key, value in {"HONEYNET_DWS_ATOMIC": "1", "RESEARCH_UNIFIED_PUBLICATION": "1",
                       "RESEARCH_ADAPTIVE_ENABLED": "0", "BUSINESS_STATE_FILE": str(checkpoint),
                       "RESEARCH_STATE_FILE": str(tmp_path / "research.sqlite"),
                       "HONEYNET_COWRIE_EXPORT": str(projection)}.items():
        monkeypatch.setenv(key, value)
    for attr, value in [("_engine", None), ("_audit_rows", []), ("_events", []), ("_event_ids", set())]:
        monkeypatch.setattr(visitor, attr, value)
    return checkpoint, projection


def wait_for(check):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        if value := check():
            return value
        time.sleep(.05)
    raise AssertionError("DWS publication did not reach the expected state")


def committed_graph(checkpoint, version, kind):
    state = atomic_state.read(checkpoint)
    assert version in state["private_versions"]
    (fact,) = [row["payload"]["data"] for facts in state["facts"]["object"].values() for row in facts
               if row["payload"]["type"] == "generated_graph" and row["world_version"] == version
               and row["payload"]["data"]["kind"] == kind]
    return fact


def test_model_authored_entities_and_services_gate_visibility_on_dws(monkeypatch, tmp_path):
    checkpoint, projection = setup_environment(monkeypatch, tmp_path)
    with TestClient(visitor.app) as client:
        lab, model = current_lab(), GroundedModel()
        engine = visitor.get_engine()
        run = lab.create(engine.world, "plain", model)["run_id"]
        root = f"/decoy/{run}"
        assert client.get(root + "/service/catalog/check").status_code == 404
        lab.activate(run)
        initial = lab.release_status(run)["manifest"]["graph_world_version"]
        graph = committed_graph(checkpoint, initial, "scene")
        from research.artifacts import scene_summary

        summary = scene_summary(run, initial)
        assert summary["version"] == initial and "content" not in summary["nodes"][0]
        assert any(edge["kind"] == "hosted_on" for edge in graph["edges"])
        assert any(node["kind"] == "http_route" and node["path"] == "catalog/check" for node in graph["nodes"])
        assert client.get(root + "/service/catalog/check").json()["data"] == {"status": "ready"}
        session = client.cookies.get("research_session")
        row = lab.plan(run)
        def fail_second_projection(db, schema, payload):
            store(db, schema, payload)
            if schema == "ssh":
                failed.set()
                raise OSError("simulated DWS projection failure")

        failed = Event()
        store = atomic_state._store
        with monkeypatch.context() as patch:
            patch.setattr(atomic_state, "_store", fail_second_projection)
            submitted = adapt(lab, run, session, model, interval=1)
            assert submitted["status"] == "publishing"
            assert failed.wait(timeout=5)
            assert atomic_state.read(checkpoint)["meta"]["world_version"] == initial
            assert atomic_state.read(projection)["world_version"] == initial
            assert not lab.policy(run, session)
            assert client.get(root + "/service/change/check").status_code == 404
        version = wait_for(lambda: lab.policy(run, session).get("dws_version"))
        assert version > initial
        assert committed_graph(checkpoint, version, "adaptation")["base"] == row["world_version"]
        assert client.get(root + "/service/change/check").json()["data"] == {"status": "reviewed"}
        assert TestClient(visitor.app).get(root + "/service/change/check").status_code == 404
        assert client.post(root + "/service/change/check").status_code == 404
        matching = [event for event in lab.report(run)["events"] if event["what"]["resource"] == "service/change/check"]
        assert [model_event(event, row)["what"]["activity"] for event in matching] == [
            "unknown", "service_access", "unknown", "unknown",
        ]
        assert matching[1]["dws_version"] == version


def test_candidate_route_is_verified_on_separate_visitor_listener(monkeypatch, tmp_path):
    from research import service_planner

    setup_environment(monkeypatch, tmp_path)
    lab, world = current_lab(), visitor.get_engine().world
    with listener("business.app:app", dict(os.environ, HONEYNET_VISITOR_ONLY="1"), ingress_only=True) as public:
        monkeypatch.setenv("HONEYNET_VISITOR_HEALTH_PORT", str(public.base_url.port))
        first = lab.create(world, "plain", GroundedModel())["run_id"]
        assert lab.activate(first)["active_run"] == first
        assert lab.report(first)["events"] == []
        assert public.get(f"/decoy/{first}/service/catalog/check").json()["data"] == {"status": "ready"}
        candidate = lab.create(world, "plain", GroundedModel())["run_id"]
        route = f"/decoy/{candidate}/service/catalog/check"
        assert public.get(route, headers={service_planner.PROBE_HEADER: "f" * 64}).status_code == 404
        prepare = service_planner.prepare_release

        def broken_manifest(*args, **kwargs):
            manifest = prepare(*args, **kwargs)
            manifest["routes"][0]["path"] += "-broken"
            return manifest

        monkeypatch.setattr(service_planner, "prepare_release", broken_manifest)
        with pytest.raises(service_planner.ReleasePreparationError) as exc:
            lab.activate(candidate)
        assert exc.value.code == "route_unready"
        assert lab.active() == first
        assert lab.release_status(candidate)["attempts"][0]["detail"] == "route_unready"
        assert public.get(route).status_code == 404
        assert public.get(f"/decoy/{first}/service/catalog/check").status_code == 200
        with lab.connect() as db:
            assert db.execute("SELECT count(*) FROM release_probes").fetchone() == (0,)


def test_publication_lanes_continue_after_failure(monkeypatch):
    from business import graph_publications, publications

    monkeypatch.setenv("RESEARCH_UNIFIED_PUBLICATION", "1")
    monkeypatch.setattr(visitor, "get_engine", lambda: None)
    monkeypatch.setattr("research.api.current_lab", lambda: None)
    completed = []

    def unavailable(*args):
        raise OSError("simulated unavailable document queue")

    monkeypatch.setattr(publications, "apply_next", unavailable)
    monkeypatch.setattr(graph_publications, "apply_next", lambda *args: completed.append("graph"))
    monkeypatch.setattr(artifacts, "archive_expired", lambda *args: completed.append("archive"))
    monkeypatch.setattr(artifacts, "apply_next", lambda *args: completed.append("generated"))
    visitor._apply_publication()
    assert completed == ["graph", "archive", "generated"]


def test_protocol_decoys_are_grounded_and_projected_after_commit(monkeypatch, tmp_path):
    checkpoint, projection = setup_environment(monkeypatch, tmp_path)
    with TestClient(visitor.app):
        visitor.get_engine()
        lab = current_lab()
        for entry, activity in (("ssh", "inspect_logs"), ("mysql", "list_catalog"), ("llm", "doc")):
            session = entry + "session"
            protocol_decoys.observe(lab, entry, session, activity)
            assert protocol_decoys.process_one(lab, GroundedProtocolModel())
            released = wait_for(lambda: protocol_decoys.available(lab, entry, session))
            graph = committed_graph(checkpoint, released["dws_version"], "protocol")
            assert graph["nodes"][1]["id"] == released["entity_id"]
            assert {"source": entry + "_review", "target": released["entity_id"], "kind": "documents"} in graph["edges"]
            if related := released.get("related_entity_id"):
                source = protocol_graph(visitor.get_engine().world, graph["base"], entry)
                expected = [dict(source=edge["source"], target=edge["target"], kind=edge["relation"])
                            for edge in source["relations"] if {edge["source"], edge["target"]}
                            == {released["entity_id"], related}]
                assert expected and all(edge in graph["edges"] for edge in expected)
            assert protocol_decoys.available(lab, entry, "different") is None
        path = projection.parent / "session-decoys" / (hashlib.sha256(b"ssh:sshsession").hexdigest() + ".json")
        assert json.loads(path.read_bytes())["title"] == "ssh_review"


def test_generated_fact_claims_reject_conflicts_and_render_checked_values(monkeypatch, tmp_path):
    checkpoint, _ = setup_environment(monkeypatch, tmp_path)
    with TestClient(visitor.app) as client:
        lab = current_lab()
        run = lab.create(visitor.get_engine().world, "plain", GroundedModel())["run_id"]
        plan = lab.plan(run)
        graph = plan["entity_graph"]
        entities = {entity["id"]: entity["label"] for entity in graph["entities"]}
        edge = next(edge for edge in graph["relations"] if edge["relation"] == "hosted_on")
        scene = plan["scene"]
        scene["nodes"][0]["body"] = "Deployment review for this component hosted on nonexistent-host99."
        with pytest.raises(ValueError, match="structured DWS claims"):
            validate_scene(scene, plan["documents"], graph)
        scene["nodes"][0]["body"] = "部署核验：[[dws:declared-host]]"
        scene["fact_claims"] = [dict(id="declared-host", entity_id=edge["source"], field="hosted_on",
                                    value="incorrect host")]
        with pytest.raises(ValueError, match="contradicts"):
            validate_scene(scene, plan["documents"], graph)
        expected = entities[edge["target"]]
        scene["fact_claims"][0]["value"] = expected
        scene["services"][0]["data"]["host"] = "wrong-value"
        with pytest.raises(ValueError, match="matching DWS claim"):
            validate_scene(scene, plan["documents"], graph)
        scene["services"][0]["data"]["host"] = "[[dws:declared-host]]"
        with lab.connect() as db:
            db.execute("UPDATE runs SET plan=? WHERE id=?", (json.dumps(plan), run))
        lab.activate(run)
        assert client.get(f"/decoy/{run}/index").json()["content"] == "部署核验：" + expected
        assert client.get(f"/decoy/{run}/service/catalog/check").json()["data"]["host"] == expected
        version = lab.graph_version(run)
        assert committed_graph(checkpoint, version, "scene")["fact_claims"][0]["value"] == expected


def test_scene_degree_counts_incoming_navigation_and_service_edges(monkeypatch, tmp_path):
    setup_environment(monkeypatch, tmp_path)
    lab = current_lab()
    run = lab.create(visitor.get_engine().world, "plain", GroundedModel())["run_id"]
    plan = lab.plan(run)
    scene = plan["scene"]
    checked = validate_scene(scene, plan["documents"], plan["entity_graph"])
    non_service_degree = sum(edge["kind"] != "serves" and "evidence" in (edge["source"], edge["target"])
                             for edge in scene_edges(checked, plan["entity_graph"]))
    scene["services"] = [{"id": f"check-{index}", "method": "GET", "path": f"check/{index}",
                          "node_id": "evidence", "data": {}} for index in range(MAX_SCENE_DEGREE + 1 - non_service_degree)]
    with pytest.raises(ValueError, match="node degree limit"):
        validate_scene(scene, plan["documents"], plan["entity_graph"])
    scene["services"].pop()
    assert validate_scene(scene, plan["documents"], plan["entity_graph"])


def test_expired_private_graphs_release_budget_and_preserve_pins_and_backup(monkeypatch, tmp_path):
    from dws.world import MAX_WORLD_NODES

    checkpoint, projection = setup_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(checkpoint.parent / "research.sqlite"))
    engine, lab = visitor.get_engine(), current_lab()
    world = engine.world
    start = world.total_nodes()
    now = int(time.time())
    stamp = [now]
    monkeypatch.setattr(artifacts, "time", lambda: stamp[0])
    ground = artifacts.protocol_graph(world, world.meta["world_version"], "ssh")["entities"][0]["id"]

    def publish(subject, expires):
        with lab.connect() as db:
            identifier = artifacts.stage(db, "protocol", subject, 1, world.meta,
                                         {"entry": "ssh", "decoy": {"title": "synthetic_note",
                                          "note": "Synthetic test note", "intent": "documentation",
                                          "entity_id": ground}}, expires=expires)
        artifacts.apply_next(lab, engine, visitor._checkpoint)
        with lab.connect() as db:
            return artifacts.release(db, identifier)[:2]

    statuses = [publish(str(i), now + 20) for i in range(MAX_WORLD_NODES - start)]
    assert all(status == "active" for status, _ in statuses)
    assert publish("would-exceed-budget", now + 20) == ("rejected", None)
    versions = [version for _, version in statuses]
    first, pinned = versions[:2]
    with lab.connect() as db:
        db.execute("INSERT INTO version_bindings VALUES ('pinned','run',?,?,?)",
                   (world.meta["world_id"], 1, now + 60))
        db.execute("INSERT INTO binding_overlays VALUES ('pinned',?)", (pinned,))
    stamp[0] += 25
    original = atomic_state._store

    def fail_projection(db, schema, payload):
        original(db, schema, payload)
        if schema == "ssh":
            raise OSError("simulated archive transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(atomic_state, "_store", fail_projection)
        with pytest.raises(OSError, match="simulated archive"):
            artifacts.archive_expired(lab, engine, visitor._checkpoint)
    assert world.total_nodes() == MAX_WORLD_NODES
    assert atomic_state.archived_graph(checkpoint, first) is None
    assert atomic_state.read(projection)["world_version"] == world.meta["world_version"]
    with lab.connect() as db:
        assert db.execute("SELECT status FROM generated_releases WHERE world_version=?", (first,)).fetchone() == ("active",)

    for _ in range(4):
        artifacts.archive_expired(lab, engine, visitor._checkpoint)
    assert world.total_nodes() == start + 1
    assert atomic_state.archived_graph(checkpoint, pinned) is None
    original_fact = atomic_state.archived_graph(checkpoint, first)
    assert original_fact["payload"]["data"]["nodes"][0]["content"] == "Synthetic test note"
    assert artifacts.graph_summary(first)["archived"] is True
    assert artifacts.graph_summary(pinned)["archived"] is False
    status, renewed = publish("0", now + 90)
    assert status == "active"
    live = atomic_state.read(checkpoint)
    (new_fact,) = [row for rows in live["facts"]["object"].values() for row in rows if row["world_version"] == renewed]
    assert new_fact["fact_id"] != original_fact["fact_id"]
    backup = tmp_path / "backup.sqlite"
    with closing(sqlite3.connect(checkpoint)) as source, closing(sqlite3.connect(backup)) as copy:
        source.backup(copy)
    assert atomic_state.archived_graph(backup, first) == original_fact
    assert atomic_state.read(backup)["meta"]["world_version"] == renewed
    bundle, recovered = tmp_path / "bundle", tmp_path / "recovered"
    assert snapshot_bundle(checkpoint.parent, projection, bundle)["world_version"] == renewed
    restore_bundle(bundle, recovered)
    assert atomic_state.archived_graph(recovered / "state" / "world.sqlite", first) == original_fact
    assert atomic_state.read(recovered / "cowrie" / "world.sqlite")["world_version"] == renewed
    from research.lab import ResearchLab

    restored_lab = ResearchLab(recovered / "state" / "research.sqlite")
    with restored_lab.connect() as db:
        assert db.execute("SELECT status FROM generated_releases WHERE world_version=?", (first,)).fetchone() == ("archived",)
    damaged = bundle / "state" / "world.sqlite"
    with damaged.open("ab") as file:
        file.write(b"corruption probe")
    with pytest.raises(ValueError, match="integrity"):
        restore_bundle(bundle, tmp_path / "failed-restore")


@pytest.mark.parametrize("runtime,reason", [("HTTP", "standalone_http"), ("MYSQL", "sql_transactions")])
def test_model_service_preapproves_one_isolated_runtime_and_recovers(runtime, reason, monkeypatch, tmp_path):
    image = os.environ.get(f"HONEYNET_TEST_L3_{runtime}_IMAGE")
    if not image:
        pytest.skip("requires approved local isolated runtime image")
    setup_environment(monkeypatch, tmp_path)
    allow = "isolated_" + runtime.lower()
    monkeypatch.setenv("RESEARCH_AUTO_SERVICE_CAPABILITIES", allow)
    monkeypatch.setenv(f"HONEYNET_L3_{runtime}_IMAGE", image)

    class RuntimeModel(GroundedModel):
        def choose(self, messages):
            scene = super().choose(messages)
            if "honeynet builder AI" in messages[0]["content"]:
                scene.update(capability_requests=["isolated_instance"], isolated_reason=reason)
            return scene

    with tempfile.TemporaryDirectory(prefix="dlp-graph-runtime-") as directory, TestClient(visitor.app):
        monkeypatch.setenv("HONEYNET_L3_STATE_DIR", directory)
        lab, world = current_lab(), visitor.get_engine().world
        prior = lab.create(world, "plain", GroundedModel())["run_id"]
        lab.activate(prior)
        run = lab.create(world, "plain", RuntimeModel(), automatic=True)["run_id"]
        plan = lab.plan(run)
        instance = Path(directory) / run
        try:
            with monkeypatch.context() as denied:
                denied.setenv("RESEARCH_AUTO_SERVICE_CAPABILITIES", "")
                with pytest.raises(ValueError, match="approval changed"):
                    lab.activate(run)
                assert lab.active() == prior and not instance.exists()
            # Simulate worker loss after preparation before making the manifest active.
            identifier, version = lab._publish_scene(plan)
            isolated_runtime(plan).start(dict(plan, graph_world_version=version))
            assert lab.active() == prior
            assert current_lab().pending_automatic_run() == run
            lab.activate(run)
            manifest = lab.release_status(run)["manifest"]
            assert manifest["publication_id"] == identifier and manifest["graph_world_version"] == version
            assert manifest["new_processes"] == 1
            name = "honeynet-l3-" + run
            inspected = json.loads(subprocess.check_output(["docker", "inspect", name], text=True, timeout=10))[0]
            assert inspected["HostConfig"]["NetworkMode"] == "none"
            assert not inspected["HostConfig"]["PortBindings"]
            if runtime == "HTTP":
                transport = httpx.HTTPTransport(uds=str(instance / "socket" / "http.sock"))
                with httpx.Client(transport=transport, trust_env=False) as client:
                    body = client.get("http://local/index.html").text
                    assert f'data-world-version="{version}"' in body
                    assert "独立生成的业务叙事 initial" in body
            else:
                with pymysql.connect(unix_socket=str(instance / "socket" / "mysql.sock"), user="catalog_reader",
                                     password=(instance / "catalog.password").read_text(), database="collab_core",
                                     read_timeout=3) as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT world_version FROM projection")
                    assert cursor.fetchone() == (version,)
                    cursor.execute("SELECT body FROM generated_nodes WHERE node_id='overview'")
                    assert cursor.fetchone() == ("独立生成的业务叙事 initial",)
            subprocess.run(["docker", "stop", name], capture_output=True, check=True, timeout=20)
            assert lab.compensate_unready() == {"status": "compensated", "active_run": prior}
            assert not instance.exists()
        finally:
            isolated_runtime(plan).stop(run)
