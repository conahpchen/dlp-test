"""Opt-in real SSH verification of version-pinned Cowrie files."""

import copy
import os
import select
import shutil
import socket
import subprocess
import time
from pathlib import Path
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from business.atomic_state import read
from business.enterprise import enrich_seed
from business.storage import persist
from entries.gateway import app as gateway
from dws.world import World
from replay.engine import Engine
from research.api import current_lab
from research.bindings import issue
from tests.test_generative_research import SceneModel
from tests.test_ingress_isolation import mysql_listener
from tests.test_v2_loopback import listener


@pytest.mark.skipif(not os.environ.get("HONEYNET_TEST_COWRIE_IMAGE"), reason="requires local Cowrie image")
def test_real_cowrie_keeps_old_dws_projection(demo_seed, tmp_path, monkeypatch):
    engine = Engine(World(enrich_seed(demo_seed)))
    directory = tmp_path / "projection"
    projection = directory / "world.sqlite"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(tmp_path / "private" / "world.sqlite"))
    monkeypatch.setenv("HONEYNET_COWRIE_EXPORT", str(projection))
    monkeypatch.setenv("HONEYNET_COWRIE_HOST", "web01")
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setattr(gateway, "_engine", engine)
    api = TestClient(gateway.app)
    headers = {"Authorization": "Bearer sk-demo-internal-2026", "X-Session": "before-publication"}
    assert api.get("/v1/models", headers=headers).json()["data"][0]["id"] == "corp-llm-7b"
    persist(engine, [], [])
    snapshot = read(projection)
    assert set(snapshot) == {"schema", "world_version", "hostname", "files"}
    assert snapshot["world_version"] == engine.world.meta["world_version"]
    lab = current_lab()
    run = lab.create(engine.world, "plain", model=SceneModel())["run_id"]
    lab.activate(run)
    binding = issue(lab, run)
    assert binding["ssh_environment"] == "HONEYNET_BINDING"
    directory.chmod(0o755)
    hook = tmp_path / "hook"
    hook.mkdir()
    hook.chmod(0o755)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "entries/cowrie/runtime/start.py", hook / "start.py")
    (hook / "start.py").chmod(0o644)
    userdb = tmp_path / "userdb.txt"
    userdb.write_text("root:x:synthetic-cowrie-test\n")
    userdb.chmod(0o644)
    askpass = tmp_path / "askpass"
    askpass.write_text('#!/bin/sh\nprintf "%s\\n" synthetic-cowrie-test\n')
    askpass.chmod(0o700)
    isolated = os.environ.get("HONEYNET_TEST_COWRIE_ISOLATED_IMAGE")
    image = isolated or os.environ["HONEYNET_TEST_COWRIE_IMAGE"]
    command = [] if isolated else [
        "/dws-hook/start.py", "/cowrie/cowrie-env/bin/twistd", "-n", "--umask=0022", "--pidfile=",
        "--logger", "cowrie.python.logfile.stdoutLogger", "cowrie",
    ]
    container = subprocess.check_output([
        "docker", "run", "--pull=never", "-d", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-p", "127.0.0.1::2222",
        "-e", "HONEYNET_COWRIE_PROJECTION=/projection/world.sqlite",
        "-e", "COWRIE_HONEYPOT_HOSTNAME=web01",
        "-e", "RESEARCH_AUTOMATIC_DECOYS=1",
        "-e", "HONEYNET_SSH_COMMANDS_PER_10S=80",
        "--mount", f"type=bind,source={hook},target=/dws-hook,readonly",
        "--mount", f"type=bind,source={directory},target=/projection,readonly",
        "--mount", f"type=bind,source={userdb},target=/cowrie/cowrie-git/etc/userdb.txt,readonly",
        image, *command,
    ], text=True, timeout=40).strip()
    sessions = []
    try:
        address = subprocess.check_output(["docker", "port", container, "2222/tcp"], text=True, timeout=5)
        port = int(address.strip().rsplit(":", 1)[-1])
        deadline = time.monotonic() + 20
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1) as probe:
                    if probe.recv(256).startswith(b"SSH-"):
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(subprocess.check_output(["docker", "logs", container], stderr=subprocess.STDOUT,
                                                    text=True, timeout=5))
            time.sleep(.25)

        def connect(binding_token=None):
            env = dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", DISPLAY="test")
            if binding_token:
                env["HONEYNET_BINDING"] = binding_token
            child = subprocess.Popen([
                "ssh", "-F", "/dev/null", "-tt", "-o", "ConnectTimeout=4",
                "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={tmp_path / 'known_hosts'}",
                "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1",
                "-o", "SendEnv=HONEYNET_BINDING",
                "-p", str(port), "root@127.0.0.1",
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env)
            sessions.append(child)
            return child

        def commands(child, command):
            child.stdin.write((command + "\necho __projection_done__\n").encode())
            child.stdin.flush()
            end = time.monotonic() + 8
            data = b""
            while time.monotonic() < end:
                if not select.select([child.stdout], [], [], max(0, end - time.monotonic()))[0]:
                    break
                chunk = os.read(child.stdout.fileno(), 16384)
                if not chunk:
                    break
                data += chunk
                if b"__projection_done__\r\n" in data and data.count(b"__projection_done__") >= 2:
                    break
            return data.decode(errors="replace")

        if isolated:
            probe = """
import errno, socket
try:
    socket.create_connection(("127.0.0.1", 2222), timeout=1)
except OSError as exc:
    assert exc.errno == errno.EPERM
else:
    raise AssertionError("unexpected outbound connection")
"""
            probe_file = hook / "probe.py"
            probe_file.write_text(probe)
            probe_file.chmod(0o644)
            subprocess.run([
                "docker", "exec", container, "/cowrie/cowrie-env/bin/python3",
                "/cowrie/dws-network.py", "/dws-hook/probe.py",
            ], check=True, timeout=10)

        old = connect()
        result = commands(old, "ls /home/deploy; cat /home/deploy/notes.txt")
        assert "notes.txt" in result and "rotate ssh keys" in result, result
        result = commands(old, "touch /home/deploy/session-only; ls /home/deploy")
        assert any("session-only" in line and "touch " not in line for line in result.splitlines()), result
        if isolated:
            from entries.cowrie.bridge import main as bridge
            from research.protocol_decoys import process_one
            import re

            class ConfigurationModel:
                def choose(self, _messages):
                    return {"intent": "configuration", "title": "ssh_inventory",
                            "note": "Internal service configuration reference."}

            monkeypatch.setenv("RESEARCH_AUTOMATIC_DECOYS", "1")
            commands(old, "ls /etc; cat /etc/service-reference.conf")
            raw = tmp_path / "cowrie.json"
            subprocess.run(["docker", "cp", f"{container}:/cowrie/cowrie-git/var/log/cowrie/cowrie.json",
                            str(raw)], check=True, timeout=10)
            assert bridge(["--in", str(raw), "--alerts-only", "--canary-state", str(lab.path)]) == 0
            assert process_one(lab, ConfigurationModel())
            result = commands(old, "ls /etc; cat /etc/service-reference.conf")
            assert "name=ssh_inventory" in result, result
            assert re.search(r"username=svc_[a-f0-9]{10}", result)
            assert re.search(r"password=decoy_[a-f0-9]{24}", result)
            class LogModel:
                def choose(self, messages):
                    import json

                    history = json.loads(messages[-1]["content"])["activities"]
                    assert history[-1] == "inspect_logs"
                    assert "choose the intent of the most recent explicit lookup" in messages[0]["content"]
                    return {"intent": "log_review", "title": "service_inventory",
                            "note": "Inventory refresh completed."}

            before = commands(old, "cat /var/log/service-reference.log")
            assert "No such file" in before
            subprocess.run(["docker", "cp", f"{container}:/cowrie/cowrie-git/var/log/cowrie/cowrie.json",
                            str(raw)], check=True, timeout=10)
            bridge(["--in", str(raw), "--alerts-only", "--canary-state", str(lab.path)])
            assert process_one(lab, LogModel())
            result = commands(old, "cat /var/log/service-reference.log")
            assert "INFO service_inventory: Inventory refresh completed." in result, result
            assert "password=" not in result

            another = connect()
            assert "name=ssh_inventory" not in commands(another, "cat /etc/service-reference.conf")
            assert "Inventory refresh completed" not in commands(another, "cat /var/log/service-reference.log")

        note = next(f for f in engine.world.all_current_cls("data")
                    if f.payload["path"] == "/home/deploy/notes.txt")
        ai_host = next(row for row in engine.world.all_current_cls("host")
                       if row.payload["hostname"] == "ai01")
        new_host = copy.deepcopy(ai_host.payload)
        next(service for service in new_host["services"] if service["proto"] == "openai")["ver"] = "corp-new"
        database = next(row for row in engine.world.all_current_cls("object") if row.payload["type"] == "database")
        prior_engine = database.payload["data"]["engine"]
        catalog = dict(database.payload, data=dict(database.payload["data"], engine="MySQL 8.0.99"))
        engine.commit_external(None, [
            ("data", dict(note.payload, content="new-dws-fact")), ("host", new_host), ("object", catalog),
        ])
        persist(engine, [], [])
        assert api.get("/v1/models", headers=headers).json()["data"][0]["id"] == "corp-llm-7b"
        assert api.get("/v1/models").json()["data"][0]["id"] == "corp-new"
        result = commands(old, "cat /home/deploy/notes.txt")
        assert "rotate ssh keys" in result and "new-dws-fact" not in result, result
        fresh = connect()
        result = commands(fresh, "ls /home/deploy; cat /home/deploy/notes.txt; cat /etc/hosts")
        assert "new-dws-fact" in result and "10.20.3.10" in result, result
        assert "session-only" not in result, result
        bound = connect(binding["binding"])
        result = commands(bound, "cat /home/deploy/notes.txt")
        if "rotate ssh keys" not in result or "new-dws-fact" in result:
            logs = subprocess.check_output(["docker", "logs", "--tail", "35", container],
                                           stderr=subprocess.STDOUT, text=True, timeout=5)
            pytest.fail((result + logs).replace(binding["binding"], "[binding]"))
        if isolated:
            from business.graph_publications import GraphProposal, apply_next, submit, transition
            from entries.mysql.app import QUERY
            from research.bindings import HEADER

            graph = submit(GraphProposal.model_validate(dict(
                base_version=binding["world_version"], scope="branch",
                nodes=[dict(id="runtime_notes", kind="document", content="Synthetic graph runtime notes"),
                       dict(id="runtime_catalog", kind="database")],
                edges=[dict(source="runtime_notes", target="runtime_catalog")],
            )))
            transition(graph["id"], "approve")
            apply_next(engine, lambda current: persist(current, [], []))
            branch = issue(lab, run, graph["id"], target="ssh")
            assert branch["branch_version"] > branch["world_version"]
            path = "/kb/graph-runtime_notes.md"
            assert "Synthetic graph runtime notes" not in commands(fresh, f"cat {path}")
            assert "Synthetic graph runtime notes" in commands(connect(branch["binding"]), f"cat {path}")

            successor = lab.create(engine.world, "plain", model=SceneModel())["run_id"]
            lab.activate(successor)
            common = dict(os.environ)
            for name in ("RESEARCH_MODEL_API_KEY", "HONEYNET_CONTROL_TOKEN", "HONEYNET_DASHBOARD_PASSWORD"):
                common.pop(name, None)
            common["HONEYNET_PG_DSN"] = ""
            control_headers = {"x-control-token": "synthetic-control"}
            auth = ("operator", "synthetic-dashboard")
            with ExitStack() as live:
                public = live.enter_context(listener(
                    "business.app:app", dict(common, HONEYNET_VISITOR_ONLY="1"), ingress_only=True
                ))
                control = live.enter_context(listener(
                    "research.control_app:app", dict(common, HONEYNET_CONTROL_TOKEN="synthetic-control")
                ))
                dashboard = live.enter_context(listener(
                    "management.app:app", dict(common, HONEYNET_DASHBOARD_PASSWORD="synthetic-dashboard")
                ))
                connect_mysql = live.enter_context(mysql_listener(
                    tmp_path / "private" / "world.sqlite", tmp_path / "research.sqlite"
                ))
                issued = {HEADER: binding["binding"]}
                public.cookies.clear()
                entry = public.get("/", headers=issued)
                assert entry.headers["location"].endswith(run + "/index")
                assert public.get("/v1/models", headers=dict(headers, **issued)).json()["data"][0]["id"] == "corp-llm-7b"
                with connect_mysql(binding["mysql_user"]) as db, db.cursor() as cursor:
                    cursor.execute(QUERY)
                    assert cursor.fetchone()[1] == prior_engine
                with connect_mysql() as db, db.cursor() as cursor:
                    cursor.execute(QUERY)
                    assert cursor.fetchone()[1] == "MySQL 8.0.99"
                endpoint = f"/api/evidence/runs/{run}/bindings"
                grants = {}
                for target in ("web", "mysql", "llm"):
                    response = dashboard.post(endpoint, params={"publication": graph["id"], "target": target},
                                              auth=auth, json={})
                    assert response.status_code == 201, response.text
                    grants[target] = response.json()
                assert len({row["binding"] for row in [branch, *grants.values()]}) == 4
                overlay = {HEADER: grants["web"]["binding"]}
                graph_path = f"/decoy/{run}/world-graph"
                assert public.get(graph_path, headers=issued).json() == {"graphs": []}
                assert public.get(f"/decoy/{successor}/world-graph", headers=overlay).status_code == 404
                web_graph, = public.get(graph_path, headers=overlay).json()["graphs"]
                assert web_graph["edges"] == [{"source": "runtime_notes", "target": "runtime_catalog"}]
                assert web_graph["nodes"][0]["content"] == "Synthetic graph runtime notes"
                assert web_graph["version"] == branch["branch_version"]
                assert public.get(graph_path, headers={HEADER: "unknown"}).status_code == 404
                with connect_mysql(grants["mysql"]["mysql_user"]) as db, db.cursor() as cursor:
                    cursor.execute(QUERY)
                    assert any(row[0] == "graph_runtime_catalog" for row in cursor.fetchall())
                with connect_mysql(binding["mysql_user"]) as db, db.cursor() as cursor:
                    cursor.execute(QUERY)
                    assert all(row[0] != "graph_runtime_catalog" for row in cursor.fetchall())
                assert public.get(graph_path, headers={HEADER: grants["llm"]["binding"]}).status_code == 404
                assert public.get("/v1/models", headers=dict(headers, **overlay)).status_code == 404
                import pymysql

                with pytest.raises(pymysql.OperationalError):
                    connect_mysql("catalog_" + grants["web"]["binding"])
                ai_overlay = {HEADER: grants["llm"]["binding"]}
                for token, expected in [(issued, False), (ai_overlay, True), ({}, False)]:
                    chat_headers = dict(headers, **token) if token else dict(headers, **{"X-Session": "fresh-ai"})
                    reply = public.post("/v1/chat/completions", headers=chat_headers, json={
                        "model": "corp-llm-7b" if token else "corp-new",
                        "messages": [{"role": "user", "content": "你好"}],
                    })
                    assert reply.status_code == 200, reply.text
                    output = reply.json()["choices"][0]["message"]["content"]
                    assert (path in output) == expected, output
                assert public.get("/api/research/status", headers=control_headers).status_code == 404
                assert control.get("/api/research/status", headers=control_headers).status_code == 200
                assert control.get("/v1/models").status_code == 404
                assert public.get("/api/runtime", auth=auth).status_code == 404
                assert dashboard.get("/api/runtime").status_code == 401
                persisted = read(tmp_path / "private" / "world.sqlite")
                assert dashboard.get("/api/runtime", auth=auth).json()["world_version"] == persisted["meta"]["world_version"]
                assert "rotate ssh keys" in commands(bound, "cat /home/deploy/notes.txt")

            limited = connect()
            assert "__projection_done__" in commands(limited, "echo ssh-ready")
            # Keep stdin open so the client can only exit here on server termination.
            limited.stdin.write(b"echo harmless-check\n" * 180)
            limited.stdin.flush()
            output, end = b"", time.monotonic() + 12
            while select.select([limited.stdout], [], [], max(0, end - time.monotonic()))[0]:
                chunk = os.read(limited.stdout.fileno(), 16384)
                if not chunk:
                    break
                output += chunk
                if time.monotonic() >= end:
                    break
            limited.wait(timeout=1)
            assert limited.returncode == 255 and b"Command budget exceeded" in output
            raw = tmp_path / "policy-limit.json"
            subprocess.run(["docker", "cp", f"{container}:/cowrie/cowrie-git/var/log/cowrie/cowrie.json",
                            str(raw)], check=True, timeout=10)
            from audit.alerts import read_alerts
            from entries.cowrie.bridge import main as bridge

            monkeypatch.setenv("HONEYNET_ALERTS_FILE", str(tmp_path / "limits.sqlite"))
            bridge(["--in", str(raw), "--alerts-only"])
            assert any(row["kind"] == "session_ended" for row in read_alerts("ssh")["alerts"])
    finally:
        for child in sessions:
            child.terminate()
            child.communicate(timeout=5)
        subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True, check=False, timeout=15)
