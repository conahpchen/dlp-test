"""种子驱动的随机轨迹生成器（10^4 轨迹不变量验收 / 消融实验数据源）。

确定性：同一 (seed, index) 生成完全相同的 seed_dict 与事件序列——
验收可复现（R7），回放可比对（K5）。
"""
from __future__ import annotations

import hashlib
import random

from dws.ids import entity_uuid, ulid_from_seed

_HOST_POOL = ["web01", "app01", "db01", "cache01", "jump01"]
_OS_POOL = ["Ubuntu 22.04.3 LTS", "Debian 12.2", "CentOS Stream 9"]
_USER_POOL = ["root", "deploy", "intern", "ops", "svc-backup"]
_ROLES = {"root": "sysadmin", "deploy": "sysadmin", "intern": "intern",
          "ops": "sysadmin", "svc-backup": "auditor"}
NODE = "sensor-a"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _secret(seed: str, index: int, username: str, j: int) -> str:
    return "sha256:" + _sha(f"{seed}:{index}:{username}:{j}")[:40]


def generate(seed: str, index: int):
    """返回 (seed_dict, events)。每次调用结果相同。"""
    rng = random.Random(f"{seed}:traj:{index}")
    base = 1_730_000_000 + index * 1000

    def hlc(i: int) -> str:
        return f"{base + i // 8}.{(i % 8) * 1111:04d}/{NODE}"

    seed_hlc = f"{base}.0000/replay"   # 种子事实时间用世界节点（I1 now 边界）

    # -------------------------------------------------------- 世界种子
    subnet_k = rng.randint(1, 250)
    subnets = [f"10.20.{subnet_k}.0/24"]
    n_hosts = rng.randint(2, 5)
    hosts = rng.sample(_HOST_POOL, n_hosts)
    ssh_host = hosts[0]

    host_payloads = []
    for i, h in enumerate(hosts):
        services = [{"port": 22, "proto": "ssh", "ver": "OpenSSH 9.3p1", "banner": ""}]
        if h == "db01":
            services.append({"port": 3306, "proto": "mysql", "ver": "8.0.35", "banner": ""})
        host_payloads.append({"hostname": h, "ip": f"10.20.{subnet_k}.{10 + i}",
                              "os": rng.choice(_OS_POOL), "services": services,
                              "banner": None})

    users = rng.sample(_USER_POOL, rng.randint(2, 4))
    identity_payloads = [
        {"username": u, "role": _ROLES[u],
         "role_bindings": [{"entry": "ssh", "role": _ROLES[u]}],
         "mfa": False, "last_login_hlc": seed_hlc}
        for u in users]

    main_user = users[0]
    cred_payloads, secrets = [], {}
    for u in users:
        for j in range(rng.randint(1, 2)):
            s = _secret(seed, index, u, j)
            secrets.setdefault(u, []).append(s)
            valid_on = [{"host": h, "service": "ssh", "scope": "ro"}
                        for h in rng.sample(hosts, rng.randint(1, len(hosts)))]
            if u == main_user and ssh_host not in {v["host"] for v in valid_on}:
                valid_on.append({"host": ssh_host, "service": "ssh", "scope": "rw"})
            cred_payloads.append({"secret_hash": s,
                                  "owner_entity_id": entity_uuid("identity", u),
                                  "valid_on": valid_on, "revoked_at_hlc": None})

    data_payloads = [
        {"path": f"/home/{u}/notes.txt",
         "owner_entity_id": entity_uuid("identity", u),
         "ctime_hlc": seed_hlc, "mtime_hlc": seed_hlc, "kind": "file",
         "content": "meeting notes v1\n", "canary": False, "deleted": False}
        for u in users if u != "root"]
    data_payloads.append(
        {"path": "/srv/backup.sql", "owner_entity_id": entity_uuid("identity", users[0]),
         "ctime_hlc": seed_hlc, "mtime_hlc": seed_hlc, "kind": "file",
         "content": "INSERT INTO assets VALUES (1);\n", "canary": False, "deleted": False})
    if rng.random() < 0.5:
        data_payloads.append(
            {"path": f"/home/{main_user}/projects",
             "owner_entity_id": entity_uuid("identity", main_user),
             "ctime_hlc": seed_hlc, "mtime_hlc": seed_hlc, "kind": "dir",
             "content": None, "canary": False, "deleted": False})

    links: dict = {}
    for i in range(len(hosts) - 1):
        links.setdefault((hosts[i], hosts[i + 1]), set()).add(22)
    if "db01" in hosts and hosts[0] != "db01":
        links.setdefault((hosts[0], "db01"), set()).update({22, 3306})
    relation_payloads = [
        {"from_entity_id": entity_uuid("host", a), "to_entity_id": entity_uuid("host", b),
         "kind": "l3-reachable", "ports": sorted(ports)}
        for (a, b), ports in links.items()]

    seed_dict = {
        "meta": {"seed": f"{seed}:{index}", "node": "replay", "subnets": subnets,
                 "base_seconds": base, "initial_version": 1},
        "initial_facts": {"host": host_payloads, "identity": identity_payloads,
                          "credential": cred_payloads, "data": data_payloads,
                          "relation": relation_payloads},
    }

    # -------------------------------------------------------- 事件序列
    events = []
    counter = 0

    def add(src_ip24, session, who, what):
        nonlocal counter
        events.append({
            "event_id": ulid_from_seed(f"{seed}:evt:{index}:{counter}"),
            "ts_hlc": hlc(counter),
            "entry": "ssh",
            "node": NODE,
            "dst": {"host": ssh_host, "port": 22},
            "session": session,
            "attacker_id": None,
            "src": {"ip": src_ip24},
            "who": who,
            "what": what,
            "verdict": None,
            "dws_version": None,
            "evidence_ref": f"synthetic://{seed}/{index}/{counter}",
        })
        counter += 1

    main_ip = f"{rng.randint(1, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.0/24"
    main_cred = secrets[main_user][0]

    for k in range(rng.randint(1, 2)):
        sid = f"s{index}x{k}"
        add(main_ip, sid, {"user": None, "cred_hash": None},
            {"kind": "ssh.connection"})
        for _ in range(rng.randint(0, 3)):
            add(main_ip, sid, {"user": "admin", "cred_hash": "sha256:wrong"},
                {"kind": "ssh.login_fail", "attempt_user": "admin",
                 "attempt_password_hash": "sha256:wrong"})
        if rng.random() < 0.9:
            add(main_ip, sid, {"user": main_user, "cred_hash": main_cred},
                {"kind": "ssh.login", "attempt_user": main_user,
                 "attempt_password_hash": main_cred})
            writes = 0
            for c in range(rng.randint(1, 12)):
                op = rng.choice(["whoami", "uname", "cat_passwd", "cat_hosts",
                                 "cat_file", "cat_missing", "ls_home", "ls_root",
                                 "echo", "echo_append", "touch", "mkdir", "rm",
                                 "ping_ok", "ping_db", "ping_unknown",
                                 "ssh_pivot", "unknown"])
                home = f"/home/{main_user}"
                if op == "whoami":
                    cmd = "whoami"
                elif op == "uname":
                    cmd = "uname -a"
                elif op == "cat_passwd":
                    cmd = "cat /etc/passwd"
                elif op == "cat_hosts":
                    cmd = "cat /etc/hosts"
                elif op == "cat_file":
                    cmd = f"cat {home}/notes.txt"
                elif op == "cat_missing":
                    cmd = f"cat {home}/nope_{c}.txt"
                elif op == "ls_home":
                    cmd = f"ls -la {home}"
                elif op == "ls_root":
                    cmd = "ls /"
                elif op == "echo":
                    if writes >= 9:
                        cmd = "whoami"
                    else:
                        writes += 1
                        cmd = f"echo report {index}-{c} > {home}/f{c}.txt"
                elif op == "echo_append":
                    if writes == 0:
                        cmd = "whoami"
                    else:
                        cmd = f"echo more >> {home}/f0.txt"
                elif op == "touch":
                    if writes >= 9:
                        cmd = "whoami"
                    else:
                        writes += 1
                        cmd = f"touch {home}/t{c}.txt"
                elif op == "mkdir":
                    if writes >= 10:
                        cmd = "whoami"
                    else:
                        writes += 2
                        cmd = f"mkdir {home}/d{c}"
                elif op == "rm":
                    cmd = f"rm {home}/t{max(0, c - 1)}.txt"
                elif op == "ping_ok":
                    cmd = f"ping {ssh_host}"
                elif op == "ping_db":
                    cmd = "ping db01" if "db01" in hosts else f"ping {hosts[-1]}"
                elif op == "ping_unknown":
                    cmd = f"ping nosuch{c}.fake"
                elif op == "ssh_pivot":
                    cmd = f"ssh root@{hosts[-1]}"
                else:
                    cmd = f"figlet hello{c}"
                add(main_ip, sid, {"user": main_user, "cred_hash": main_cred},
                    {"kind": "ssh.command", "cmd": cmd})
        add(main_ip, sid, {"user": main_user, "cred_hash": main_cred},
            {"kind": "ssh.close"})

    if rng.random() < 0.3:  # 第二攻击者的短噪声块
        other_ip = f"{rng.randint(1, 223)}.{rng.randint(0, 255)}.0/24".replace(
            ".0/24", f".{rng.randint(0, 255)}.0/24")
        sid = f"s{index}y"
        add(other_ip, sid, {"user": None, "cred_hash": None},
            {"kind": "ssh.connection"})
        for _ in range(rng.randint(1, 2)):
            add(other_ip, sid, {"user": "admin", "cred_hash": "sha256:bad"},
                {"kind": "ssh.login_fail", "attempt_user": "admin",
                 "attempt_password_hash": "sha256:bad"})
        add(other_ip, sid, {"user": None, "cred_hash": None},
            {"kind": "ssh.close"})

    return seed_dict, events
