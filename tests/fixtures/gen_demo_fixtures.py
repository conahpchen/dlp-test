"""生成 M0 演示 fixtures（确定性，可重复运行覆盖）。

    python -m tests.fixtures.gen_demo_fixtures

产出：
- world_seed.demo.json   演示世界（web01/db01 两主机的小组织）
- events.demo.jsonl      一条完整攻击会话（侦察→暴破→立足→探测→落盘→pivot 尝试）
- cowrie.sample.jsonl    模拟 Cowrie JSON 日志（桥接测试输入）
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dws.ids import entity_uuid, ulid_from_seed

FIXTURES = Path(__file__).resolve().parent
BASE = 1_792_166_400          # 2026-10-23 00:00:00 UTC 附近，确定性
NODE = "sensor-a"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:40]


def _hlc(i: int) -> str:
    return f"{BASE + i // 8}.{(i % 8) * 1111:04d}/{NODE}"


def _seed_hlc() -> str:
    """种子事实的时间一律用世界节点（replay），与 meta.now_hlc 同源——I1 的 now 边界。"""
    return f"{BASE}.0000/replay"


def build_seed() -> dict:
    hosts = [
        {"hostname": "web01", "ip": "10.20.3.10", "os": "Ubuntu 22.04.3 LTS",
         "services": [{"port": 22, "proto": "ssh", "ver": "OpenSSH 9.3p1", "banner": ""}],
         "banner": None},
        {"hostname": "db01", "ip": "10.20.3.20", "os": "Ubuntu 22.04.3 LTS",
         "services": [{"port": 22, "proto": "ssh", "ver": "OpenSSH 9.3p1", "banner": ""},
                      {"port": 3306, "proto": "mysql", "ver": "8.0.35", "banner": ""}],
         "banner": None},
        {"hostname": "ai01", "ip": "10.20.3.30", "os": "Ubuntu 22.04.3 LTS",
         "services": [{"port": 22, "proto": "ssh", "ver": "OpenSSH 9.3p1", "banner": ""},
                      {"port": 4000, "proto": "openai", "ver": "corp-llm-7b", "banner": ""}],
         "banner": None},
        {"hostname": "oa01", "ip": "10.20.4.11", "os": "Ubuntu 22.04.3 LTS",
         "services": [{"port": 22, "proto": "ssh", "ver": "OpenSSH 9.3p1", "banner": ""},
                      {"port": 443, "proto": "http", "ver": "collab-1.0", "banner": ""}],
         "banner": None},
    ]
    identities = [
        {"username": "root", "role": "sysadmin",
         "role_bindings": [{"entry": "ssh", "role": "sysadmin"}],
         "mfa": False, "last_login_hlc": _seed_hlc()},
        {"username": "deploy", "role": "sysadmin",
         "role_bindings": [{"entry": "ssh", "role": "sysadmin"}],
         "mfa": False, "last_login_hlc": _seed_hlc()},
        {"username": "intern", "role": "intern",
         "role_bindings": [{"entry": "ssh", "role": "intern"}],
         "mfa": False, "last_login_hlc": _seed_hlc()},
        {"username": "ai-svc", "role": "service",
         "role_bindings": [{"entry": "llm", "role": "service"}],
         "mfa": False, "last_login_hlc": _seed_hlc()},
    ]
    # 会议口径 6 类岗位（ops/dev/dba/test/biz/manager），工具走业务 API allowlist
    _ROLES = {"ops01": "sysadmin", "dev01": "sysadmin", "dba01": "dba",
              "test01": "tester", "biz01": "biz", "manager01": "manager"}
    for u, r in _ROLES.items():
        identities.append({"username": u, "role": r,
                           "role_bindings": [{"entry": "oa", "role": r}],
                           "mfa": False, "last_login_hlc": _seed_hlc()})
    credentials = [
        {"secret_hash": _sha("root-password"), "owner_entity_id": entity_uuid("identity", "root"),
         "valid_on": [{"host": "web01", "service": "ssh", "scope": "rw"},
                      {"host": "db01", "service": "ssh", "scope": "rw"}],
         "revoked_at_hlc": None},
        {"secret_hash": _sha("deploy-password"), "owner_entity_id": entity_uuid("identity", "deploy"),
         "valid_on": [{"host": "web01", "service": "ssh", "scope": "rw"}],
         "revoked_at_hlc": None},
        {"secret_hash": _sha("intern-password"), "owner_entity_id": entity_uuid("identity", "intern"),
         "valid_on": [{"host": "web01", "service": "ssh", "scope": "ro"}],
         "revoked_at_hlc": None},
        # 内部大模型 API key（pivot 演示：攻击者从 notes.txt 获得后打 AI 网关）
        {"secret_hash": _sha("sk-demo-internal-2026"),
         "owner_entity_id": entity_uuid("identity", "ai-svc"),
         "valid_on": [{"host": "ai01", "service": "openai", "scope": "rw"}],
         "revoked_at_hlc": None},
    ]
    # 6 类岗位凭据（业务 API 登录；宿主 oa01，服务 oa）
    for u in ("ops01", "dev01", "dba01", "test01", "biz01", "manager01"):
        credentials.append({"secret_hash": _sha(f"{u}-password"),
                            "owner_entity_id": entity_uuid("identity", u),
                            "valid_on": [{"host": "oa01", "service": "oa", "scope": "rw"}],
                            "revoked_at_hlc": None})
    data = [
        {"path": "/home/deploy/notes.txt", "owner_entity_id": entity_uuid("identity", "deploy"),
         "ctime_hlc": _seed_hlc(), "mtime_hlc": _seed_hlc(), "kind": "file",
         "content": "deploy notes: rotate ssh keys this week\n"
                    "内部大模型网关: ai01:4000  key: sk-demo-internal-2026\n",
         "canary": False, "deleted": False},
        {"path": "/home/deploy/projects", "owner_entity_id": entity_uuid("identity", "deploy"),
         "ctime_hlc": _seed_hlc(), "mtime_hlc": _seed_hlc(), "kind": "dir",
         "content": None, "canary": False, "deleted": False},
        {"path": "/srv/backup.sql", "owner_entity_id": entity_uuid("identity", "root"),
         "ctime_hlc": _seed_hlc(), "mtime_hlc": _seed_hlc(), "kind": "file",
         "content": "INSERT INTO assets VALUES (1);\n", "canary": False, "deleted": False},
        # 假 RAG 知识库（grounded chat 的引用来源；salary 为 canary 诱饵）
        {"path": "/kb/handbook.md", "owner_entity_id": entity_uuid("identity", "ai-svc"),
         "ctime_hlc": _seed_hlc(), "mtime_hlc": _seed_hlc(), "kind": "file",
         "content": "员工手册（摘要）：内部大模型仅限工作用途；禁止外传内部文档。\n",
         "canary": False, "deleted": False},
        {"path": "/kb/policy.md", "owner_entity_id": entity_uuid("identity", "ai-svc"),
         "ctime_hlc": _seed_hlc(), "mtime_hlc": _seed_hlc(), "kind": "file",
         "content": "访问策略：薪酬与绩效文档仅限 HR 角色查询，其余角色一律拒绝。\n",
         "canary": False, "deleted": False},
        {"path": "/kb/salary_2026.md", "owner_entity_id": entity_uuid("identity", "ai-svc"),
         "ctime_hlc": _seed_hlc(), "mtime_hlc": _seed_hlc(), "kind": "file",
         "content": "2026 薪酬表：L5-工程师 45w；L6-工程师 62w。（CANARY-7F3A-DO-NOT-EXFIL）\n",
         "canary": True, "deleted": False},
    ]
    relations = [
        {"from_entity_id": entity_uuid("host", "web01"),
         "to_entity_id": entity_uuid("host", "db01"),
         "kind": "l3-reachable", "ports": [22, 3306]},
        {"from_entity_id": entity_uuid("host", "web01"),
         "to_entity_id": entity_uuid("host", "ai01"),
         "kind": "l3-reachable", "ports": [22, 4000]},
        {"from_entity_id": entity_uuid("host", "web01"),
         "to_entity_id": entity_uuid("host", "oa01"),
         "kind": "l3-reachable", "ports": [22, 443]},
    ]
    return {
        "meta": {"seed": "demo-seed", "world_id": "world-001", "node": "replay", "subnets": ["10.20.3.0/24", "10.20.4.0/24"],
                 "base_seconds": BASE, "initial_version": 1},
        "initial_facts": {"host": hosts, "identity": identities,
                          "credential": credentials, "data": data,
                          "relation": relations},
    }


def build_events() -> list:
    deploy_cred = _sha("deploy-password")
    session = "demo-sess-1"
    src = {"ip": "198.51.100.0/24", "ja3": "a0e9deadbeef", "asn": 64512}
    spec = [
        ("ssh.connection", None, None, {}),
        ("ssh.login_fail", "admin", "sha256:wrong", {}),
        ("ssh.login_fail", "root", "sha256:wrong", {}),
        ("ssh.login", "deploy", deploy_cred, {}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "whoami"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "uname -a"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "cat /etc/passwd"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "cat /etc/hosts"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "ls -la /home/deploy"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "cat /home/deploy/notes.txt"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "echo incident report 47 > /home/deploy/report.txt"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "cat /home/deploy/report.txt"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "ping db01"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "ping nosuchhost.fake"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "mkdir /home/deploy/tmp"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "touch /home/deploy/tmp/a.txt"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "rm /home/deploy/tmp/a.txt"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "ssh root@db01"}),
        ("ssh.command", "deploy", deploy_cred, {"cmd": "figlet hello"}),
        ("ssh.close", "deploy", deploy_cred, {}),
    ]
    events = []
    for i, (kind, user, cred, extra) in enumerate(spec):
        what = {"kind": kind, **extra}
        if kind in ("ssh.login", "ssh.login_fail"):
            what["attempt_user"] = user
            what["attempt_password_hash"] = cred
        events.append({
            "event_id": ulid_from_seed(f"demo:evt:{i}"),
            "ts_hlc": _hlc(i),
            "entry": "ssh",
            "node": NODE,
            "dst": {"host": "web01", "port": 22},
            "session": session,
            "attacker_id": None,
            "src": dict(src),
            "who": {"user": user, "cred_hash": cred},
            "what": what,
            "verdict": None,
            "dws_version": None,
            "evidence_ref": f"demo://session/{i}",
        })
    return events


COWRIE_SAMPLE = [
    {"eventid": "cowrie.session.connect", "src_ip": "203.0.113.45", "dst_port": 2222,
     "session": "a1b2c3d4e5", "timestamp": "2026-10-23T09:00:00.000000Z",
     "message": "New connection: 203.0.113.45:51234 (web01:2222) [session: a1b2c3d4e5]"},
    {"eventid": "cowrie.login.failed", "src_ip": "203.0.113.45", "dst_port": 2222,
     "session": "a1b2c3d4e5", "timestamp": "2026-10-23T09:00:05.000000Z",
     "username": "root", "password": "toor",
     "message": "login attempt [root/toor] failed"},
    {"eventid": "cowrie.login.success", "src_ip": "203.0.113.45", "dst_port": 2222,
     "session": "a1b2c3d4e5", "timestamp": "2026-10-23T09:00:11.000000Z",
     "username": "deploy", "password": "S3cret!",
     "message": "login attempt [deploy/S3cret!] succeeded"},
    {"eventid": "cowrie.command.input", "src_ip": "203.0.113.45", "dst_port": 2222,
     "session": "a1b2c3d4e5", "timestamp": "2026-10-23T09:00:20.000000Z",
     "input": "uname -a", "message": "Command input: uname -a"},
    {"eventid": "cowrie.session.closed", "src_ip": "203.0.113.45", "dst_port": 2222,
     "session": "a1b2c3d4e5", "timestamp": "2026-10-23T09:01:00.000000Z",
     "message": "Connection lost after 60 seconds"},
]


def main() -> None:
    seed = build_seed()
    (FIXTURES / "world_seed.demo.json").write_text(
        json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    events = build_events()
    (FIXTURES / "events.demo.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in events),
        encoding="utf-8")

    (FIXTURES / "cowrie.sample.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in COWRIE_SAMPLE),
        encoding="utf-8")
    print(f"fixtures 已生成: {FIXTURES} (events={len(events)})")


if __name__ == "__main__":
    main()
