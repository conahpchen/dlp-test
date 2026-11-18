"""Opt-in Cowrie container and OpenSSH client acceptance."""

import asyncio
import os
import socket
import subprocess
import time

import pymysql
import pytest
from fastapi.testclient import TestClient

from business import app as business
from entries.cowrie.bridge import main
from entries.mysql.app import create_server
from research.api import current_lab


@pytest.mark.skipif(not os.environ.get("HONEYNET_TEST_COWRIE_IMAGE"), reason="requires local Cowrie image")
def test_http_canary_correlates_real_cowrie_mysql_and_fake_ai(tmp_path, monkeypatch):
    image = os.environ["HONEYNET_TEST_COWRIE_IMAGE"]
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(tmp_path / "world.json"))
    monkeypatch.setenv("HONEYNET_PG_DSN", "")
    monkeypatch.setenv("HONEYNET_ENABLE_MYSQL_CANARY", "1")
    monkeypatch.setattr(business, "_engine", None)
    lab = current_lab()
    run = lab.create(business.get_engine().world, "agent_note")["run_id"]
    client = TestClient(business.app)
    response = client.get(f"/decoy/{run}/documents/0")
    assert response.status_code == 200
    link = response.json()["verification_link"]
    token = link.rsplit("/", 1)[-1]
    assert client.get(link).status_code == 200
    fake = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + token},
                       json={"model": "corp-llm-7b"})
    assert fake.status_code == 401

    credentials = response.json()["mysql_verification"]

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
        "docker", "run", "--rm", "-d", "-p", "127.0.0.1::2222",
        "--mount", f"type=bind,source={userdb},target=/cowrie/cowrie-git/etc/userdb.txt,readonly", image,
    ], text=True, timeout=15).strip()
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
