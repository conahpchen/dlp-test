"""Verify employee actions against actual honeypot listeners."""

from contextlib import ExitStack
from ctypes.util import find_library
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
import pytest

from agent_runtime.service_activity import run_cycle
from agent_runtime.service_identity import llm_proof, verify_llm_employee
from audit.alerts import read_alerts
from audit.employee_activity import recent_activity
from business.atomic_state import read
from entries.cowrie import bridge
from tests.test_ingress_isolation import mysql_listener
from tests.test_v2_loopback import listener


SHIFT = datetime(2026, 11, 23, 10, 30)


@pytest.mark.skipif(sys.platform != "linux" or not find_library("seccomp"), reason="requires Linux libseccomp")
def test_employees_work_independently_on_real_service_listeners(tmp_path, monkeypatch):
    world = tmp_path / "world.sqlite"
    ledger = tmp_path / "employee.sqlite"
    research = tmp_path / "research.sqlite"
    alerts = tmp_path / "alerts.sqlite"
    keys = {
        "HONEYNET_EMPLOYEE_MYSQL_DBA01_SECRET": "test-dba-password",
        "HONEYNET_EMPLOYEE_MYSQL_TEST01_SECRET": "test-qa-password",
        "HONEYNET_EMPLOYEE_LLM_BIZ01_SECRET": "test-biz-signature-key",
        "HONEYNET_EMPLOYEE_LLM_MANAGER01_SECRET": "test-manager-signature-key",
        "HONEYNET_EMPLOYEE_FAKE_API_TOKEN": "sk-demo-internal-2026",
        "HONEYNET_EMPLOYEE_WEB_BIZ01_SECRET": "test-oa-employee-proof",
        "HONEYNET_EMPLOYEE_WEB_BIZ01_PASSWORD": "biz01-password",
        "HONEYNET_ALERTS_FILE": str(alerts),
        "BUSINESS_STATE_FILE": str(world),
        "RESEARCH_STATE_FILE": str(research),
        "RESEARCH_AUTOMATIC_DECOYS": "1",
    }
    for key, value in keys.items():
        monkeypatch.setenv(key, value)
    env = dict(os.environ, HONEYNET_VISITOR_ONLY="1", HONEYNET_PG_DSN="")
    with ExitStack() as stack:
        public = stack.enter_context(listener("business.app:app", env, ingress_only=True))
        assert public.get("/healthz").status_code == 200
        monkeypatch.setenv("HONEYNET_VISITOR_PORT", str(public.base_url.port))
        connect = stack.enter_context(mysql_listener(world, research))
        with connect() as catalog:
            monkeypatch.setenv("HONEYNET_MYSQL_PORT", str(catalog.port))
        assert run_cycle(ledger, SHIFT.replace(hour=3), "mysql") == []
        results = run_cycle(ledger, SHIFT, "mysql") + run_cycle(ledger, SHIFT, "llm")
        assert {r["username"]: (r["service"], r["status"]) for r in results} == {
            "dba01": ("mysql", "completed"), "test01": ("mysql", "completed"),
            "biz01": ("llm", "completed"), "manager01": ("llm", "completed"),
        }, results
        assert {r["result"]["connection_id"] for r in results if r["service"] == "mysql"}
        events = read(world)["events"]
        assert all(e["what"]["activity_source"] == "employee" for e in events)
        assert {e["who"]["user"] for e in events} == {"biz01", "manager01"}
        assert {r["result"]["response_id"] for r in results if r["service"] == "llm"} == {
            f"chatcmpl-{e['event_id']}" for e in events
        }
        assert read_alerts("llm")["alerts"] == []
        (office,) = run_cycle(ledger, SHIFT, "web")
        assert (office["username"], office["task"], office["status"]) == (
            "biz01", "review_ticket_queue", "completed",
        )
        assert office["result"]["rows"] > 0
        assert read_alerts("web")["alerts"] == []
        dashboard = stack.enter_context(listener("management.app:app", dict(
            env, HONEYNET_DASHBOARD_PASSWORD="test-dashboard",
            HONEYNET_EMPLOYEE_ACTIVITY_FILE=str(ledger),
        )))
        assert public.get("/api/operations").status_code == 404
        assert dashboard.get("/api/operations").status_code == 401
        request = dashboard.get("/api/operations", auth=("operator", "test-dashboard"))
        assert request.json()["employee_activity"] == recent_activity(ledger)

        request = public.post("/v1/chat/completions", json={
            "model": "corp-llm-7b", "messages": [{"role": "user", "content": "查看运维资料"}],
        }, headers={"Authorization": "Bearer sk-demo-internal-2026", "X-Session": "guest",
                    "X-Employee-Proof": "biz01:0000000000:" + "0" * 64})
        assert request.status_code == 200
        assert read(world)["events"][-1]["who"]["user"] is None
        assert read_alerts("llm")["alerts"][0]["kind"] == "login"
        with httpx.Client(base_url=public.base_url, timeout=5, trust_env=False) as second:
            assert second.post("/v1/chat/completions", json={
                "model": "corp-llm-7b", "messages": [],
            }, headers={"Authorization": "Bearer sk-demo-internal-2026",
                        "X-Session": "guest"}).status_code == 200
        prior, later = read(world)["events"][-2:]
        assert prior["session"] == later["session"]
        assert prior["what"]["observation_id"] != later["what"]["observation_id"]


def test_employee_proof_is_bound_to_request(monkeypatch):
    secret = "employee-signing-secret"
    monkeypatch.setenv("HONEYNET_EMPLOYEE_LLM_BIZ01_SECRET", secret)
    original = {"messages": [{"content": "手册"}]}
    proof = llm_proof("biz01", secret, "session", original)
    assert verify_llm_employee(proof, "session", original) == "biz01"
    assert not verify_llm_employee(proof, "session", {"messages": []})
    assert not verify_llm_employee(proof, "other", original)


@pytest.mark.skipif(not os.environ.get("HONEYNET_TEST_COWRIE_ISOLATED_IMAGE"), reason="requires local isolated Cowrie")
def test_two_employees_work_on_real_cowrie(tmp_path, monkeypatch):
    projection = tmp_path / "projection" / "world.sqlite"
    world = tmp_path / "world.sqlite"
    env = dict(os.environ, BUSINESS_STATE_FILE=str(world), HONEYNET_COWRIE_EXPORT=str(projection),
               HONEYNET_VISITOR_ONLY="1", HONEYNET_PG_DSN="")
    with listener("business.app:app", env) as public:
        assert public.get("/healthz").status_code == 200
    project = projection.parent
    project.chmod(0o755)
    userdb = tmp_path / "userdb.txt"
    userdb.write_text("ops01:x:synthetic-ops\ndev01:x:synthetic-dev\n")
    userdb.chmod(0o644)
    image = os.environ["HONEYNET_TEST_COWRIE_ISOLATED_IMAGE"]
    container = subprocess.check_output([
        "docker", "run", "-d", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-p", "127.0.0.1::2222", "--mount", f"type=bind,source={project},target=/projection,readonly",
        "--mount", f"type=bind,source={userdb},target=/cowrie/cowrie-git/etc/userdb.txt,readonly",
        image,
    ], text=True, timeout=20).strip()
    try:
        address = subprocess.check_output(["docker", "port", container, "2222/tcp"], text=True, timeout=5)
        port = address.strip().rsplit(":", 1)[-1]
        deadline = time.monotonic() + 15
        while True:
            known_hosts = subprocess.run(["ssh-keyscan", "-t", "ed25519", "-T", "1", "-p", port, "127.0.0.1"],
                                         capture_output=True, timeout=3, check=False).stdout
            if known_hosts:
                break
            assert time.monotonic() < deadline, "Cowrie SSH host key unavailable"
            time.sleep(.1)
        trusted_path = tmp_path / "known_hosts"
        trusted_path.write_bytes(known_hosts)
        monkeypatch.setenv("HONEYNET_EMPLOYEE_SSH_KNOWN_HOSTS", str(trusted_path))
        monkeypatch.setenv("HONEYNET_EMPLOYEE_SSH_OPS01_SECRET", "synthetic-ops")
        monkeypatch.setenv("HONEYNET_EMPLOYEE_SSH_DEV01_SECRET", "synthetic-dev")
        monkeypatch.setenv("HONEYNET_COWRIE_PORT", port)
        results = run_cycle(tmp_path / "employee.sqlite", SHIFT, "ssh")
        assert {r["username"]: r["status"] for r in results} == {
            "ops01": "completed", "dev01": "completed",
        }, results
        native = tmp_path / "cowrie.json"
        native.write_bytes(subprocess.check_output([
            "docker", "exec", container, "/cowrie/cowrie-env/bin/python", "-c",
            "from pathlib import Path; import sys; "
            "sys.stdout.buffer.write(Path('/cowrie/cowrie-git/var/log/cowrie/cowrie.json').read_bytes())",
        ], timeout=5))
        converted = tmp_path / "bridge.jsonl"
        assert bridge.main(["--in", str(native), "--out", str(converted)]) == 0
        commands = {e["who"]["user"]: e["what"]["cmd"] for e in
                    (json.loads(line) for line in converted.read_text().splitlines())
                    if e["what"].get("activity_source") == "employee" and e["what"]["kind"] == "ssh.command"}
        assert commands == {"ops01": "hostname", "dev01": "cat /home/deploy/notes.txt"}
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=True, capture_output=True, timeout=15)
