"""行为分析规则 R001–R006（契约 14 §08，规则查表 v0，确定性）。

输入为统一事件列表；输出标签 [{event_id, rule, class}]。
"""

from __future__ import annotations


def _cmd(e: dict) -> str:
    return (e.get("what") or {}).get("cmd") or ""


def _port(e: dict):
    return (e.get("dst") or {}).get("port")


def _ip(e: dict) -> str:
    return (e.get("src") or {}).get("ip") or "?"


def apply_rules(events: list[dict], world=None) -> list[dict]:
    labels: list[dict] = []

    def tag(e, rule, cls):
        labels.append({"event_id": e["event_id"], "rule": rule, "class": cls})

    # R001: 同源探测 ≥4 个不同端口
    by_src: dict[str, set] = {}
    for e in events:
        if (e.get("what") or {}).get("kind") in ("ssh.connection", "web.request"):
            p = _port(e)
            if p:
                by_src.setdefault(_ip(e), set()).add(p)
    for ip, ports in by_src.items():
        if len(ports) >= 4:
            for e in events:
                if _ip(e) == ip and (e.get("what") or {}).get("kind") in (
                        "ssh.connection", "web.request"):
                    tag(e, "R001", "service_discovery")

    # R002: 同会话认证失败 ≥3
    fails: dict[str, list] = {}
    for e in events:
        if (e.get("what") or {}).get("kind") in ("ssh.login_fail", "llm.auth_fail"):
            fails.setdefault(e.get("session") or "?", []).append(e)
    for sess, evs in fails.items():
        if len(evs) >= 3:
            for e in evs:
                tag(e, "R002", "credential_attack_suspected")

    # R003: 敏感文件探索
    for e in events:
        cmd = _cmd(e)
        if any(k in cmd for k in ("/etc/shadow", ".env", "id_rsa")):
            tag(e, "R003", "file_discovery")

    # R004: 数据库探索迹象
    for e in events:
        cmd = _cmd(e)
        if any(k in cmd for k in ("mysql", "3306", "数据库")):
            tag(e, "R004", "database_discovery")

    # R005: 与 DWS 拓扑不一致（world 可选提供）
    if world is not None:
        known = {f.payload["hostname"] for f in world.all_current_cls("host")}
        for e in events:
            cmd = _cmd(e)
            for k in ("ping ", "ssh "):
                if cmd.startswith(k):
                    target = cmd[len(k):].strip().split()[-1].split("@")[-1]
                    if target and target not in known:
                        tag(e, "R005", "world_consistency_alert")

    # R006: 同一凭据哈希跨入口出现 → 候选关联（不直接断言）
    by_cred: dict[str, set] = {}
    for e in events:
        cred = (e.get("who") or {}).get("cred_hash")
        if cred:
            by_cred.setdefault(cred, set()).add(e.get("entry"))
    multi = {c for c, entries in by_cred.items() if len(entries) > 1}
    if multi:
        for e in events:
            cred = (e.get("who") or {}).get("cred_hash")
            if cred in multi:
                tag(e, "R006", "cross_entry_candidate")

    return labels
