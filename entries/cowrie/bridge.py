"""Cowrie 事件桥接：Cowrie JSON 日志 → 统一事件 v1（契约 12 §1）。

文件模式（默认）:  python -m entries.cowrie.bridge --in cowrie.json --out events.jsonl
跟随模式（部署）:  python -m entries.cowrie.bridge --in cowrie.json --out events.jsonl --follow
Redis 模式:        python -m entries.cowrie.bridge --in cowrie.json --redis redis://127.0.0.1:6379/0

映射表（M0 最小集，M1 追加 llm/web 入口 = 允许的扩展）:
  cowrie.session.connect  -> ssh.connection
  cowrie.login.failed     -> ssh.login_fail
  cowrie.login.success    -> ssh.login
  cowrie.command.input    -> ssh.command
  cowrie.session.closed   -> ssh.close

合规（T6, 12 §9）: src.ip /24 截断后存储；password 立即哈希（sha256:...），
明文不进入任何输出（事件、日志、错误信息）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import sys
import time
from collections import OrderedDict
from hmac import compare_digest
from pathlib import Path

from audit.alerts import record as record_alert
from audit.flow_evidence import observe_cowrie
from agent_runtime.service_identity import service_secret
from dws.ids import ulid_now

_KIND_MAP = {
    "cowrie.session.connect": "ssh.connection",
    "cowrie.login.failed": "ssh.login_fail",
    "cowrie.login.success": "ssh.login",
    "cowrie.command.input": "ssh.command",
    "cowrie.session.closed": "ssh.close",
}


def _ip24(ip: str) -> str:
    try:
        iface = ipaddress.ip_interface(f"{ip}/24")
        return f"{iface.network.network_address}/24"
    except ValueError:
        return "0.0.0.0/24"


def _hash_secret(text: str | None) -> str | None:
    if text is None:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:40]


def _hlc(ts_iso: str, counter: int, node: str) -> str:
    ts = ts_iso.replace("Z", "+00:00")
    epoch = int(_dt.datetime.fromisoformat(ts).timestamp())
    return f"{epoch}.{counter % 10_000:04d}/{node}"


def convert(record: dict, counter: int, node: str, dst_host: str) -> dict | None:
    kind = _KIND_MAP.get(record.get("eventid"))
    if kind is None:
        return None
    user = record.get("username")
    password_hash = _hash_secret(record.get("password"))
    what: dict = {"kind": kind}
    if kind in ("ssh.login_fail", "ssh.login"):
        what["attempt_user"] = user
        what["attempt_password_hash"] = password_hash
    elif kind == "ssh.command":
        what["cmd"] = record.get("input") or ""
    return {
        "event_id": ulid_now(),                 # 采集侧允许真随机（ids.py 说明）
        "ts_hlc": _hlc(record.get("timestamp") or
                       _dt.datetime.now(_dt.timezone.utc).isoformat(), counter, node),
        "entry": "ssh",
        "node": node,
        "dst": {"host": dst_host, "port": int(record.get("dst_port") or 22)},
        "session": record.get("session") or "unknown",
        "attacker_id": None,                    # 身份解析由引擎侧最小版回填（12 §2.3）
        "src": {"ip": _ip24(record.get("src_ip") or "0.0.0.0")},
        "who": {"user": user, "cred_hash": password_hash},
        "what": what,
        "verdict": None,
        "dws_version": None,
        "evidence_ref": f"cowrie://{record.get('session')}/{counter}",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="entries.cowrie.bridge")
    parser.add_argument("--in", dest="src", required=True, help="cowrie.json 路径")
    parser.add_argument("--out", dest="out", default=None, help="统一事件 JSONL 输出路径")
    parser.add_argument("--redis", default=None, help="redis://host:6379/0（可选）")
    parser.add_argument("--node", default="sensor-a")
    parser.add_argument("--dst-host", default="web01", help="该入口对应的假主机名")
    parser.add_argument("--follow", action="store_true", help="跟随追加（部署模式）")
    parser.add_argument("--canary-state", help="本地研究 SQLite 凭据账本")
    parser.add_argument("--alerts-only", action="store_true", help="采集告警，无需文件或 Redis 输出")
    args = parser.parse_args(argv)

    if not args.out and not args.redis and not args.alerts_only:
        parser.error("需要 --out 或 --redis 之一")

    lab = None
    if args.canary_state:
        from research.lab import ResearchLab

        lab = ResearchLab(args.canary_state)

    sink = None
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_fh = out_path.open("a", encoding="utf-8", newline="\n")
    if args.redis:
        try:
            import redis
        except ImportError:
            print("error: redis 模式需要 pip install redis", file=sys.stderr)
            return 2
        sink = redis.Redis.from_url(args.redis)

    flow_path = os.environ.get("HONEYNET_FLOW_STATE_FILE")
    flow_key = os.environ.get("HONEYNET_FLOW_HMAC_KEY", "")
    flow_interface = os.environ.get("HONEYNET_FLOW_INTERFACE", "")
    counter = 0
    skipped = 0
    employee_sessions = OrderedDict()

    src_path = Path(args.src)
    if args.follow and not src_path.exists():
        # 部署模式：Cowrie 首个事件到来才会创建日志文件，等待其就绪
        print(f"bridge: 等待日志文件就绪 {src_path} …", flush=True)
        while not src_path.exists():
            time.sleep(1.0)
        print("bridge: 日志文件已就绪", flush=True)

    def handle(line: str) -> None:
        nonlocal counter, skipped
        line = line.strip()
        if not line:
            return
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            return
        if record.get("eventid") == "cowrie.session.handoff":
            from research.handoffs import enabled, ssh_receipt

            session, parent, child = (record.get(key, "") for key in ("session", "parent_id", "child_id"))
            if (lab and enabled() and all(isinstance(value, str) for value in (session, parent, child))
                    and re.fullmatch(r"[a-f0-9]{12}", session)
                    and all(re.fullmatch(r"[a-f0-9]{64}", value) for value in (parent, child))):
                ssh_receipt(lab, args.node, session, parent, child)
            return
        if record.get("eventid") == "cowrie.session.policy_limit":
            key = json.dumps([args.node, record.get("session"), record.get("timestamp"), "session_ended"])
            record_alert("ssh", "session_ended", hashlib.sha256(key.encode()).hexdigest())
            return
        event = convert(record, counter, args.node, args.dst_host)
        if event is None:
            skipped += 1
            return
        session = record.get("session")
        if event["what"]["kind"] == "ssh.login" and session:
            username = record.get("username")
            password = record.get("password")
            expected = service_secret("ssh", username) if isinstance(username, str) else ""
            if expected and isinstance(password, str) and compare_digest(expected.encode(), password.encode()):
                employee_sessions[session] = username
                employee_sessions.move_to_end(session)
                if len(employee_sessions) > 2048:
                    employee_sessions.popitem(last=False)
        employee = employee_sessions.get(session)
        if employee:
            event["who"]["user"] = employee
            event["what"]["activity_source"] = "employee"
        if event["what"]["kind"] == "ssh.close":
            employee_sessions.pop(session, None)
        kind = {"ssh.connection": "connection", "ssh.login": "login",
                "ssh.login_fail": "login_failed"}.get(event["what"]["kind"])
        if kind == "connection" and flow_path:
            try:
                observe_cowrie(flow_path, flow_key, flow_interface, args.node, record)
            except (OSError, sqlite3.Error):
                print("bridge: network evidence store unavailable", file=sys.stderr)
        if kind and not employee:
            key = json.dumps([args.node, record.get("session"), record.get("timestamp"), kind])
            record_alert("ssh", kind, hashlib.sha256(key.encode()).hexdigest())
        if lab and not employee and event["what"]["kind"] in ("ssh.login", "ssh.login_fail"):
            password = record.get("password")
            session = record.get("session")
            if isinstance(password, str) and re.fullmatch(r"[A-Za-z0-9_-]{32}", password) and session:
                try:
                    lab.observe_lure_use(password, "ssh", f"{args.node}/{session}")
                except sqlite3.Error:
                    print("bridge: Canary evidence store unavailable", file=sys.stderr)
        if lab and not employee and event["what"]["kind"] == "ssh.command":
            from research.protocol_decoys import observe, ssh_activity
            from research.worker import automatic_enabled

            session = record.get("session")
            if automatic_enabled() and isinstance(session, str) and session:
                try:
                    observe(lab, "ssh", session, ssh_activity(record.get("input")))
                except sqlite3.Error:
                    print("bridge: protocol observation unavailable", file=sys.stderr)
        counter += 1
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
        if args.out:
            out_fh.write(payload + "\n")
        if sink is not None:
            sink.publish("events.raw.ssh", payload)

    fh = open(args.src, "r", encoding="utf-8", errors="replace")
    for line in fh:
        handle(line)
    if args.out:
        out_fh.flush()
    if not args.follow:
        fh.close()
        if args.out:
            out_fh.close()
        print(f"bridge: 转换 {counter} 条事件（跳过 {skipped} 行非映射记录）")
        return 0

    # 跟随模式：文件保持打开，轮询追加（部署期 Cowrie 持续写 cowrie.json）
    try:
        while True:
            for line in fh:
                handle(line)
            if args.out:
                out_fh.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        fh.close()
        print(f"bridge: 退出，共转换 {counter} 条（跳过 {skipped}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
