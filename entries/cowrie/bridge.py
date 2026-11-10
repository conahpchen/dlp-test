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
import sys
import time
from pathlib import Path

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
    args = parser.parse_args(argv)

    if not args.out and not args.redis:
        parser.error("需要 --out 或 --redis 之一")

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

    counter = 0
    skipped = 0

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
        event = convert(record, counter, args.node, args.dst_host)
        if event is None:
            skipped += 1
            return
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
