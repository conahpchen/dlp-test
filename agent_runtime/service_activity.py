"""Scheduled employees interacting with the actual local honeypot listeners."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pymysql

from agent_runtime.runtime import in_window, to_minute
from agent_runtime.service_identity import llm_proof, service_secret, web_proof
from audit.employee_activity import record_activity
from contracts.agent_personas import load_personas
from dws.ids import ulid_now
from entries.mysql.app import QUERY

SERVICES = {"ssh": ("HONEYNET_COWRIE_PORT", 2222), "mysql": ("HONEYNET_MYSQL_PORT", 3307),
            "llm": ("HONEYNET_VISITOR_PORT", 8080), "web": ("HONEYNET_VISITOR_PORT", 8080)}
SSH_TASKS = {"check_host": "hostname", "review_deploy_notes": "cat /home/deploy/notes.txt"}
MYSQL_TASKS = {"review_catalog": QUERY, "check_readiness": "SELECT 1"}
LLM_TASKS = {"read_handbook": "请提供运维手册和业务支持说明。",
             "review_service_inventory": "请汇总平台服务和资产目录。"}


def _endpoint(service):
    return os.environ.get(f"HONEYNET_EMPLOYEE_{service.upper()}_HOST", "127.0.0.1")


def _ssh(port, username, secret, task, _trace):
    if "\n" in secret or "\r" in secret:
        raise ValueError("Invalid SSH password")
    hosts = Path(os.environ.get("HONEYNET_EMPLOYEE_SSH_KNOWN_HOSTS", "")).resolve()
    if not hosts.is_file():
        raise ValueError("Cowrie known_hosts is required")
    with tempfile.TemporaryDirectory(prefix="honeynet-ssh-") as directory:
        askpass = Path(directory) / "askpass"
        askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$HONEYNET_EMPLOYEE_ASKPASS"\n')
        askpass.chmod(0o700)
        result = subprocess.run(
            ["ssh", "-F", "/dev/null", "-o", "StrictHostKeyChecking=yes", "-o",
             f"UserKnownHostsFile={hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
             "-o", "PasswordAuthentication=yes", "-o", "PubkeyAuthentication=no",
             "-o", "KbdInteractiveAuthentication=no", "-o", "NumberOfPasswordPrompts=1",
             "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=3", "-p", str(port),
             f"{username}@{_endpoint('ssh')}", SSH_TASKS[task]],
            stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=8,
            env={"PATH": os.defpath, "DISPLAY": "honeynet", "SSH_ASKPASS": str(askpass),
                 "SSH_ASKPASS_REQUIRE": "force", "HONEYNET_EMPLOYEE_ASKPASS": secret},
        )
    output = result.stdout.strip()
    if not output or output.startswith(b"cat:"):
        raise ValueError("Cowrie did not return the requested item")
    if task == "check_host" and output != os.environ.get("HONEYNET_COWRIE_HOST", "web01").encode():
        raise ValueError("Unexpected Cowrie hostname")
    return {"output_bytes": len(result.stdout)}


def _mysql(port, username, secret, task, _trace):
    with pymysql.connect(host=_endpoint("mysql"), port=port, user=username, password=secret,
                         connect_timeout=3, read_timeout=4, write_timeout=4) as db:
        with db.cursor() as cursor:
            cursor.execute(MYSQL_TASKS[task])
            rows = cursor.fetchall()
            if task == "check_readiness" and rows != ((1,),):
                raise ValueError("Unexpected MySQL readiness result")
        return {"connection_id": db.thread_id(), "rows": len(rows)}


def _llm(port, username, secret, task, trace):
    token = os.environ.get("HONEYNET_EMPLOYEE_FAKE_API_TOKEN", "")
    if not token or len(secret) < 16:
        raise ValueError("Employee fake API credentials are required")
    with httpx.Client(base_url=f"http://{_endpoint('llm')}:{port}", timeout=5, trust_env=False) as client:
        model = next(row["id"] for row in client.get("/v1/models").raise_for_status().json()["data"])
        body = {"model": model, "messages": [{"role": "user", "content": LLM_TASKS[task]}]}
        response = client.post("/v1/chat/completions", json=body, headers={
            "Authorization": f"Bearer {token}", "X-Session": trace,
            "X-Employee-Proof": llm_proof(username, secret, trace, body),
        }).raise_for_status()
    reply = response.json()
    if not reply["choices"][0]["message"]["content"]:
        raise ValueError("Fake API returned no task output")
    return {"response_id": reply["id"], "model": model}


def _web(port, username, secret, task, trace):
    password = os.environ.get(f"HONEYNET_EMPLOYEE_WEB_{username.upper()}_PASSWORD", "")
    if not password or len(secret) < 16 or task != "review_ticket_queue":
        raise ValueError("OA employee credentials and assigned task are required")

    def headers(method, path):
        return {"X-Session": trace, "X-Employee-Proof": web_proof(username, secret, trace, method, path)}

    with httpx.Client(base_url=f"http://{_endpoint('web')}:{port}", timeout=5, trust_env=False) as client:
        login = client.post("/api/login", json={"username": username, "password": password},
                            headers=headers("POST", "/api/login")).raise_for_status().json()
        tickets = client.get("/api/tickets", headers={
            **headers("GET", "/api/tickets"), "Authorization": f"Bearer {login['token']}",
        }).raise_for_status().json()
    return {"rows": len(tickets), "open": sum(row["status"] == "open" for row in tickets)}


ACTIONS = {"ssh": _ssh, "mysql": _mysql, "llm": _llm, "web": _web}


def run_cycle(path, now, service=None):
    minute = now.hour * 60 + now.minute
    rows = []
    for persona in load_personas():
        windows = [(to_minute(start), to_minute(end)) for start, end in persona["schedule"]["windows"]]
        if not in_window(windows, minute):
            continue
        username = persona["username"]
        for entry, task in persona.get("service_tasks", {}).items():
            if service and service != entry:
                continue
            trace = ulid_now()
            row = {"employee_id": persona["agent_id"], "username": username,
                   "role": persona["role"], "service": entry, "task": task, "trace": trace,
                   "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            secret = service_secret(entry, username)
            if not secret:
                if entry != "web" or service == "web":
                    rows.append(dict(row, status="unconfigured"))
                continue
            setting, default = SERVICES[entry]
            try:
                port = int(os.environ.get(setting, default))
                result = ACTIONS[entry](port, username, secret, task, trace)
            except (OSError, subprocess.SubprocessError, httpx.HTTPError, pymysql.MySQLError,
                    ValueError, LookupError, StopIteration) as error:
                rows.append(dict(row, status="failed", reason=type(error).__name__))
            else:
                rows.append(dict(row, status="completed", result=result))
    record_activity(path, rows)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=SERVICES)
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--at", help="Override the simulated shift time for a single run (HH:MM)")
    args = parser.parse_args()
    if args.interval < 60 or (args.at and not args.once):
        parser.error("Use an interval of at least 60 seconds and --at only with --once")
    zone = ZoneInfo(os.environ.get("HONEYNET_EMPLOYEE_TIMEZONE", "Asia/Shanghai"))
    path = os.environ.get("HONEYNET_EMPLOYEE_ACTIVITY_FILE", "var/v2/employees.sqlite")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    while True:
        now = datetime.now(zone)
        if args.at:
            override = datetime.strptime(args.at, "%H:%M")
            now = now.replace(hour=override.hour, minute=override.minute)
        rows = run_cycle(path, now, args.service)
        print(json.dumps(rows, ensure_ascii=False), flush=True)
        if args.once:
            return int(any(row["status"] != "completed" for row in rows))
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
