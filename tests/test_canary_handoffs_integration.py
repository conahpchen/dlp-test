"""Real loopback clients verify the full directed Canary handoff."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import time

import httpx
import pymysql
import pytest
from fastapi.testclient import TestClient

from business import app as visitor
from entries.cowrie.bridge import main as bridge
from entries.mysql.app import create_server, HANDOFF_QUERY, QUERY
from management.app import app as dashboard
from research.api import current_lab
from tests.test_v2_loopback import listener


@pytest.mark.skipif(not os.environ.get("HONEYNET_TEST_COWRIE_ISOLATED_IMAGE"), reason="requires live Cowrie")
def test_real_directed_canary_chain_with_independent_ingress_issuance(tmp_path, monkeypatch):
    checkpoint = tmp_path / "private" / "world.sqlite"
    projection = tmp_path / "projection" / "world.sqlite"
    env = {
        "HONEYNET_CANARY_HANDOFFS": "1", "HONEYNET_VISITOR_ONLY": "1", "HONEYNET_PG_DSN": "",
        "BUSINESS_STATE_FILE": str(checkpoint), "HONEYNET_COWRIE_EXPORT": str(projection),
        "RESEARCH_STATE_FILE": str(tmp_path / "research.sqlite"),
        "HONEYNET_DASHBOARD_PASSWORD": "synthetic-operator",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(visitor, "_engine", None)
    lab = current_lab()
    run = lab.create(visitor.get_engine().world, "agent_note")["run_id"]
    projection.parent.chmod(0o755)
    askpass = tmp_path / "askpass"
    askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$DLP_TEST_CREDENTIAL"\n')
    askpass.chmod(0o700)
    container = subprocess.check_output([
        "docker", "run", "-d", "--rm", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-p", "127.0.0.1::2222", "-e", "HONEYNET_CANARY_HANDOFFS=1",
        "--mount", f"type=bind,source={projection.parent},target=/projection,readonly",
        os.environ["HONEYNET_TEST_COWRIE_ISOLATED_IMAGE"],
    ], text=True, timeout=20).strip()

    def connect_ssh(port, account):
        return subprocess.run([
            "ssh", "-F", "/dev/null", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={tmp_path / 'known_hosts'}", "-o", "PubkeyAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no", "-o", "NumberOfPasswordPrompts=1",
            "-p", str(port), account["username"] + "@127.0.0.1",
        ], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
            env=dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force",
                     DISPLAY="test", DLP_TEST_CREDENTIAL=account["password"]))

    try:
        mapping = subprocess.check_output(["docker", "port", container, "2222/tcp"], text=True, timeout=5)
        port = int(mapping.strip().rsplit(":", 1)[-1])
        deadline = time.monotonic() + 20
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1) as probe:
                    if probe.recv(256).startswith(b"SSH-"):
                        break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                pytest.fail("Cowrie did not start")
            time.sleep(.2)
        with listener("business.app:app", dict(os.environ, **env), ingress_only=True) as web:
            document = web.get(f"/decoy/{run}/documents/0").json()
            beacon = document["verification_link"]
            with httpx.Client(base_url=web.base_url, trust_env=False) as other:
                assert other.get(beacon).status_code == 404
            ssh = web.get(beacon).json()["next_hop"]
            assert ssh["entry"] == "ssh"
            response = web.post("/v1/chat/completions", headers={"Authorization": "Bearer " + ssh["password"]},
                                json={"model": "corp-llm-7b"})
            assert response.status_code == 401 and "next_hop" not in response.json()
            deadline = time.monotonic() + 3
            permit = projection.parent / "handoffs" / (hashlib.sha256(ssh["password"].encode()).hexdigest() + ".json")
            while not permit.is_file() and time.monotonic() < deadline:
                time.sleep(.05)
            assert permit.is_file()
            invalid = connect_ssh(port, dict(ssh, password="x" * 32))
            assert invalid.returncode == 255 and "Verification handoff " not in invalid.stderr
            proof = connect_ssh(port, ssh)
            assert proof.returncode == 255
            records = [line.removeprefix("Verification handoff ") for line in proof.stderr.splitlines()
                       if line.startswith("Verification handoff ")]
            assert len(records) == 1, "Expected an SSH verification banner"
            mysql = json.loads(records[0])
            raw = tmp_path / "cowrie.json"
            subprocess.run(["docker", "cp", f"{container}:/cowrie/cowrie-git/var/log/cowrie/cowrie.json",
                            str(raw)], check=True, timeout=10)
            assert mysql["password"] not in raw.read_text() and ssh["password"] not in raw.read_text()
            assert bridge(["--in", str(raw), "--alerts-only", "--canary-state", str(lab.path)]) == 0

            async def query_next_hop():
                server = create_server(checkpoint, "synthetic-catalog", lab, bindings=lab)
                await server.start_server(host="127.0.0.1", port=0)
                mysql_port = server.sockets()[0].getsockname()[1]

                def query():
                    def connect():
                        return pymysql.connect(host="127.0.0.1", port=mysql_port, connect_timeout=2,
                                               user=mysql["username"], password=mysql["password"])
                    with connect() as connection, connection.cursor() as cursor:
                        with pytest.raises(pymysql.MySQLError):
                            cursor.execute(QUERY)
                        cursor.execute(HANDOFF_QUERY)
                        result = cursor.fetchone()[0]
                    with pytest.raises(pymysql.MySQLError):
                        connect()
                    return result

                try:
                    return await asyncio.to_thread(query)
                finally:
                    server.close()
                    await server.wait_closed()

            key = asyncio.run(query_next_hop())
            authorize = {"Authorization": "Bearer " + key}
            reply = web.post("/v1/chat/completions", headers=authorize, json={"model": "corp-llm-7b"})
            assert reply.status_code == 401 and reply.json()["next_hop"]["entry"] == "web"
            terminal = reply.json()["next_hop"]["href"]
            assert web.get(terminal).json() == {"received": True}
            with httpx.Client(base_url=web.base_url, trust_env=False) as stranger:
                assert stranger.get(terminal).status_code == 404
            dashboard_client = TestClient(dashboard)
            evidence = dashboard_client.get(f"/api/evidence/runs/{run}/chains",
                                            auth=("operator", "synthetic-operator")).json()
            assert {(row["source_entry"], row["uses"][0]["entry"]) for row in evidence} == {
                ("web", "web"), ("web", "ssh"), ("ssh", "mysql"), ("mysql", "llm"), ("llm", "web")
            }
            assert all(key not in json.dumps(evidence) for key in (ssh["password"], mysql["password"], key))
            identifier = hashlib.sha256(beacon.rsplit("/", 1)[-1].encode()).hexdigest()
            lab.revoke_canary(identifier)
            assert web.get(terminal).status_code == 404
            assert all(row["status"] == "revoked" for row in lab.canaries(run))
            assert not permit.exists()
            route = f"/api/evidence/runs/{run}/handoff-policy"
            assert dashboard_client.put(route, json={"route": ["web", "ssh"]},
                                        auth=("operator", "synthetic-operator")).status_code == 200
            with httpx.Client(base_url=web.base_url, timeout=5, trust_env=False) as fresh:
                issued = fresh.get(f"/decoy/{run}/documents/0").json()["verification_link"]
                final_account = fresh.get(issued).json()["next_hop"]
            final_id = hashlib.sha256(final_account["password"].encode()).hexdigest()
            terminal_permit = projection.parent / "handoffs" / (final_id + ".json")
            deadline = time.monotonic() + 3
            while not terminal_permit.is_file() and time.monotonic() < deadline:
                time.sleep(.05)
            assert terminal_permit.is_file()
            accepted = connect_ssh(port, final_account)
            assert "Verification recorded." in accepted.stderr and "Verification handoff " not in accepted.stderr
            subprocess.run(["docker", "cp", f"{container}:/cowrie/cowrie-git/var/log/cowrie/cowrie.json",
                            str(raw)], check=True, timeout=10)
            assert bridge(["--in", str(raw), "--alerts-only", "--canary-state", str(lab.path)]) == 0
            assert any(hit["credential_id"] == final_id and hit["entry"] == "ssh"
                       for hit in lab.canary_uses(run))
            with lab.connect() as db:
                assert db.execute("SELECT 1 FROM canary_handoffs WHERE parent_id=?", (final_id,)).fetchone() is None
    finally:
        subprocess.run(["docker", "stop", container], check=False, timeout=15, capture_output=True)
