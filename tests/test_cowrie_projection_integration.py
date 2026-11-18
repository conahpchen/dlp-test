"""Opt-in real SSH verification of version-pinned Cowrie files."""

import copy
import os
import select
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from business.atomic_state import read
from business.storage import persist
from entries.gateway import app as gateway
from dws.world import World
from replay.engine import Engine


@pytest.mark.skipif(not os.environ.get("HONEYNET_TEST_COWRIE_IMAGE"), reason="requires local Cowrie image")
def test_real_cowrie_keeps_old_dws_projection(demo_seed, tmp_path, monkeypatch):
    engine = Engine(World(demo_seed))
    directory = tmp_path / "projection"
    projection = directory / "world.sqlite"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(tmp_path / "private" / "world.sqlite"))
    monkeypatch.setenv("HONEYNET_COWRIE_EXPORT", str(projection))
    monkeypatch.setenv("HONEYNET_COWRIE_HOST", "web01")
    monkeypatch.setattr(gateway, "_engine", engine)
    api = TestClient(gateway.app)
    headers = {"Authorization": "Bearer sk-demo-internal-2026", "X-Session": "before-publication"}
    assert api.get("/v1/models", headers=headers).json()["data"][0]["id"] == "corp-llm-7b"
    persist(engine, [], [])
    snapshot = read(projection)
    assert set(snapshot) == {"schema", "world_version", "hostname", "files"}
    assert snapshot["world_version"] == engine.world.meta["world_version"]
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
    container = subprocess.check_output([
        "docker", "run", "-d", "-p", "127.0.0.1::2222",
        "-e", "HONEYNET_COWRIE_PROJECTION=/projection/world.sqlite",
        "-e", "COWRIE_HONEYPOT_HOSTNAME=web01",
        "--mount", f"type=bind,source={hook},target=/dws-hook,readonly",
        "--mount", f"type=bind,source={directory},target=/projection,readonly",
        "--mount", f"type=bind,source={userdb},target=/cowrie/cowrie-git/etc/userdb.txt,readonly",
        os.environ["HONEYNET_TEST_COWRIE_IMAGE"], "/dws-hook/start.py",
        "/cowrie/cowrie-env/bin/twistd", "-n", "--umask=0022", "--pidfile=",
        "--logger", "cowrie.python.logfile.stdoutLogger", "cowrie",
    ], text=True, timeout=20).strip()
    sessions = []
    try:
        binding = subprocess.check_output(["docker", "port", container, "2222/tcp"], text=True, timeout=5)
        port = int(binding.strip().rsplit(":", 1)[-1])
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

        def connect():
            child = subprocess.Popen([
                "ssh", "-F", "/dev/null", "-tt", "-o", "ConnectTimeout=4",
                "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={tmp_path / 'known_hosts'}",
                "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1",
                "-p", str(port), "root@127.0.0.1",
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", DISPLAY="test"))
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

        old = connect()
        result = commands(old, "ls /home/deploy; cat /home/deploy/notes.txt")
        assert "notes.txt" in result and "rotate ssh keys" in result, result
        result = commands(old, "touch /home/deploy/session-only; ls /home/deploy")
        assert any("session-only" in line and "touch " not in line for line in result.splitlines()), result
        note = next(f for f in engine.world.all_current_cls("data")
                    if f.payload["path"] == "/home/deploy/notes.txt")
        ai_host = next(row for row in engine.world.all_current_cls("host")
                       if row.payload["hostname"] == "ai01")
        new_host = copy.deepcopy(ai_host.payload)
        next(service for service in new_host["services"] if service["proto"] == "openai")["ver"] = "corp-new"
        engine.commit_external(None, [
            ("data", dict(note.payload, content="new-dws-fact")), ("host", new_host),
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
    finally:
        for child in sessions:
            child.terminate()
            child.communicate(timeout=5)
        subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True, check=False, timeout=15)
