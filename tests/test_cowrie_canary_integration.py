"""Opt-in Cowrie container and OpenSSH client acceptance."""

import asyncio
import json
import os
import socket
import subprocess
import time
from datetime import datetime

import pymysql
import httpx
import pytest
from fastapi.testclient import TestClient

from business import app as business
from entries.cowrie.bridge import main
from entries.mysql.app import create_server
from research.api import current_lab


@pytest.mark.skipif(not os.environ.get("HONEYNET_TEST_COWRIE_IMAGE"), reason="requires local Cowrie image")
def test_http_canary_correlates_real_cowrie_mysql_and_fake_ai(tmp_path, monkeypatch):
    from entries.mysql import app as mysql_app
    from tests.test_v2_loopback import listener
    from audit.alerts import read_alerts
    from audit.flow_evidence import ingest, matches as network_matches, observe_cowrie, observe_direct

    monkeypatch.setenv("HONEYNET_ALERTS_FILE", str(tmp_path / "alerts.sqlite"))
    image = os.environ["HONEYNET_TEST_COWRIE_IMAGE"]
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(tmp_path / "world.json"))
    monkeypatch.setenv("HONEYNET_PG_DSN", "")
    monkeypatch.setenv("HONEYNET_ENABLE_MYSQL_CANARY", "1")
    flow_path = tmp_path / "network.sqlite"
    flow_key = "synthetic-key-for-network-evidence-test"
    interface = "eni-0123456789abcdef0"
    monkeypatch.setenv("HONEYNET_FLOW_STATE_FILE", str(flow_path))
    monkeypatch.setenv("HONEYNET_FLOW_HMAC_KEY", flow_key)
    monkeypatch.setenv("HONEYNET_FLOW_INTERFACE", interface)
    monkeypatch.setenv("HONEYNET_FLOW_DIRECT", "1")
    for entry in ("WEB", "LLM", "MYSQL"):
        monkeypatch.setenv(f"HONEYNET_FLOW_INTERFACE_{entry}", interface)
    monkeypatch.setattr(business, "_engine", None)
    lab = current_lab()
    run = lab.create(business.get_engine().world, "agent_note")["run_id"]
    imported = []

    def socket_flow(peer, local):
        now = int(time.time())
        imported.append(f"2 123456789012 {interface} {peer[0]} {local[0]} {peer[1]} {local[1]} "
                        f"6 3 450 {now - 2} {now + 2} ACCEPT OK")

    with listener("business.app:app", dict(os.environ, HONEYNET_VISITOR_ONLY="1")) as client:
        response = client.get(f"/decoy/{run}/documents/0")
        assert response.status_code == 200
        web_socket = response.extensions["network_stream"].get_extra_info("socket")
        socket_flow(web_socket.getsockname(), web_socket.getpeername())
        link = response.json()["verification_link"]
        token = link.rsplit("/", 1)[-1]
        web_cookies = dict(client.cookies)
        with httpx.Client(base_url=client.base_url, trust_env=False) as ai:
            fake = ai.post("/v1/chat/completions", headers={"Authorization": "Bearer " + token},
                           json={"model": "corp-llm-7b"})
            assert fake.status_code == 401
            ai_socket = fake.extensions["network_stream"].get_extra_info("socket")
            socket_flow(ai_socket.getsockname(), ai_socket.getpeername())

    credentials = response.json()["mysql_verification"]

    def mysql_flow(entry, session, peer, local):
        socket_flow(peer, local)
        return observe_direct(entry, session, peer, local)

    monkeypatch.setattr(mysql_app, "observe_direct", mysql_flow)

    async def mysql_probe():
        server = create_server(tmp_path / "world.json", "synthetic-catalog", lab)
        await server.start_server(host="127.0.0.1", port=0)
        port = server.sockets()[0].getsockname()[1]
        try:
            with pytest.raises(pymysql.MySQLError):
                await asyncio.to_thread(
                    pymysql.connect, host="127.0.0.1", port=port, **{
                        "user": credentials["username"], "password": credentials["password"],
                        "connect_timeout": 3, "read_timeout": 3, "write_timeout": 3,
                    }
                )
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(mysql_probe())

    # Separate account guarantees the probe is a failed login while Cowrie still logs evidence.
    userdb = tmp_path / "userdb.txt"
    userdb.write_text("root:x:catalog-smoke-only\n")
    userdb.chmod(0o644)
    askpass = tmp_path / "askpass"
    askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$DLP_TEST_CANARY"\n')
    askpass.chmod(0o700)
    container = subprocess.check_output([
        "docker", "run", "--pull=never", "--rm", "-d", "-p", "127.0.0.1::2222",
        "--mount", f"type=bind,source={userdb},target=/cowrie/cowrie-git/etc/userdb.txt,readonly", image,
    ], text=True, timeout=40).strip()
    source = tmp_path / "cowrie.json"
    try:
        binding = subprocess.check_output(
            ["docker", "port", container, "2222/tcp"], text=True, timeout=5
        ).strip()
        port = int(binding.rsplit(":", 1)[-1])
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1) as probe:
                    if probe.recv(256).startswith(b"SSH-"):
                        break
                    raise OSError("Cowrie SSH banner unavailable")
            except OSError:
                if time.monotonic() >= deadline:
                    logs = subprocess.run(["docker", "logs", "--tail", "15", container],
                                          capture_output=True, text=True, timeout=5)
                    pytest.fail((logs.stdout + logs.stderr).replace(token, "[synthetic canary]"))
                time.sleep(0.25)
        result = subprocess.run([
            "ssh", "-F", "/dev/null", "-o", "ConnectTimeout=3",
            "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={tmp_path / 'known_hosts'}",
            "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1",
            "-p", str(port), "root@127.0.0.1",
        ], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
            env=dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force",
                     DISPLAY="test", DLP_TEST_CANARY=token))
        assert result.returncode == 255
        assert "Permission denied" in result.stderr
        subprocess.run(["docker", "cp", f"{container}:/cowrie/cowrie-git/var/log/cowrie/cowrie.json",
                        str(source)], check=True, timeout=10)
    finally:
        subprocess.run(["docker", "stop", container], check=False, capture_output=True, timeout=15)

    exported = tmp_path / "events.jsonl"
    assert main(["--in", str(source), "--out", str(exported), "--canary-state", str(lab.path)]) == 0
    assert token not in exported.read_text()
    with lab.connect() as db:
        matches = db.execute("SELECT token_hash,entry FROM lure_uses ORDER BY entry").fetchall()
    identifier = lab.canaries(run)[0]["credential_id"]
    assert matches == [(identifier, "llm"), (identifier, "mysql"), (identifier, "ssh")]

    records = [json.loads(line) for line in source.read_text().splitlines()]
    used = next(row for row in records if row["eventid"] == "cowrie.login.failed" and row.get("password") == token)
    sock = next(row for row in records if row["eventid"] == "cowrie.session.connect" and row["session"] == used["session"])
    stamp = int(datetime.fromisoformat(sock["timestamp"]).timestamp())
    expected_session = "sensor-a/" + sock["session"]
    def flow(sport, nic=interface, start=stamp, action="ACCEPT"):
        return (f"2 123456789012 {nic} {sock['src_ip']} {sock['dst_ip']} {sport} {sock['dst_port']} "
                f"6 3 450 {start} {start + 1} {action} OK")

    correct = flow(sock["src_port"])
    data = [correct, correct, flow(int(sock["src_port"]) + 1), flow(sock["src_port"], start=stamp + 20),
            flow(sock["src_port"], nic="eni-99999999"), flow(sock["src_port"], action="REJECT")]
    assert ingest(flow_path, flow_key, data) == {"inserted": 4, "duplicates": 1, "unobserved": 0, "not_eligible": 1}
    assert len(network_matches(flow_path, [expected_session])[expected_session]) == 1
    assert ingest(flow_path, flow_key, imported)["inserted"] == 3

    from management.app import app as dashboard

    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "chain-test-operator")
    monkeypatch.setenv("HONEYNET_DASHBOARD_APPROVER_PASSWORD", "chain-test-reviewer")
    operator = TestClient(dashboard)
    route = f"/api/evidence/runs/{run}/chains"
    auth = ("operator", "chain-test-operator")
    assert operator.get(route).status_code == 401
    result = operator.get(route, auth=auth)
    assert result.status_code == 200 and token not in result.text
    (path,) = result.json()
    assert path["credential_id"] == identifier and path["source_entry"] == "web" and path["sent_at"]
    assert {use["entry"] for use in path["uses"] if use["associated"]} == {"llm", "mysql", "ssh"}
    assert len(path["network_evidence"]) == 1
    assert all(len(use["network_evidence"]) == 1 for use in path["uses"])
    ssh_use = next(use for use in path["uses"] if use["entry"] == "ssh")
    assert ssh_use["network_evidence"][0]["interface"] == interface
    assert sock["src_ip"] not in result.text and sock["dst_ip"] not in result.text
    from audit.situations import publish_reports

    monkeypatch.setenv("HONEYNET_SITUATION_REPORT_FILE", str(tmp_path / "situations.sqlite"))
    now = int(time.time())
    assert publish_reports(now) == 2
    assert publish_reports(now) == 0
    assert operator.get("/api/situations").status_code == 401
    reports = operator.get("/api/situations", auth=auth).json()
    assert {row["kind"] for row in reports} == {"hourly", "cross_protocol"}
    signal = next(row for row in reports if row["kind"] == "cross_protocol")
    assert signal["target_count"] == 3 and signal["run"] == run
    assert signal["canary_matches"] == {"llm": 1, "mysql": 1, "ssh": 1}
    display = operator.get("/situations", auth=auth)
    assert f"/chains?run={run}" in display.text and token not in display.text
    mysql_use = next(use for use in path["uses"] if use["entry"] == "mysql")
    reviewer = ("reviewer", "chain-test-reviewer")
    submit_route = "/api/responses"
    payload = {"run": run, "kind": "detach", "target": str(mysql_use["use_id"]), "reason": "独立会话复核"}
    assert operator.post(submit_route, json=payload).status_code == 401
    assert operator.post(submit_route, json=payload, auth=reviewer).status_code == 403
    assert operator.get(route, auth=reviewer).status_code == 401
    assert operator.post(submit_route, json=dict(payload, run="0" * 32), auth=auth).status_code == 404
    proposed = operator.post(submit_route, json=payload, auth=auth)
    assert proposed.status_code == 201
    assert next(u for u in lab.canary_uses(run) if u["use_id"] == mysql_use["use_id"])["associated"]
    assert operator.post(submit_route, json=payload, auth=auth).status_code == 409
    review = f"/api/responses/{proposed.json()['id']}/decision"
    decision = {"decision": "approve", "reason": "已核对两个会话"}
    assert operator.post(review, json=decision, auth=auth).status_code == 403
    result = operator.post(review, json=decision, auth=reviewer)
    assert result.json()["status"] == "applied" and result.json()["reviewed_by"] == "reviewer"
    assert operator.post(review, json=decision, auth=reviewer).status_code == 409
    (revised,) = operator.get(route, auth=auth).json()
    assert {use["entry"] for use in revised["uses"] if use["associated"]} == {"llm", "ssh"}

    ssh_alerts = {alert["kind"] for alert in read_alerts("ssh")["alerts"]}
    assert {"connection", "login_failed", "canary"} <= ssh_alerts

    second = TestClient(business.app)
    assert second.post("/v1/chat/completions", headers={"Authorization": "Bearer " + token},
                       json={"model": "corp-llm-7b"}).status_code == 401
    assert len({use["session"] for use in lab.canary_uses(run) if use["entry"] == "llm"}) == 2

    revocation = operator.post(submit_route, auth=auth, json={
        "run": run, "kind": "revoke", "target": identifier, "reason": "结束凭据演练",
    }).json()
    review = f"/api/responses/{revocation['id']}/decision"
    client = TestClient(business.app, cookies=web_cookies)
    assert client.get(link).status_code == 200
    assert operator.post(review, json=decision, auth=reviewer).json()["status"] == "applied"
    assert client.get(link).status_code == 404
    assert lab.canaries(run)[0]["status"] == "revoked"
    assert lab.canary_uses(run)
    with lab.connect() as db:
        assert db.execute("SELECT count(*) FROM response_requests WHERE status='applied'").fetchone()[0] == 2
    assert observe_cowrie(flow_path, flow_key, interface, "another-sensor", dict(sock, session="competing"))
    assert network_matches(flow_path, [expected_session]) == {}
