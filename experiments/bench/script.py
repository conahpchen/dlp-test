"""系统无关的固定攻击脚本（同脚本回放的"脚本"本体）。

确定性：同一 (seed, n_sessions) 生成完全相同的动作序列——四套蜜网吃的是
同一份脚本，CSVR 才可跨系统对比。动作是协议级描述，由各 adapter 翻译执行。
"""

from __future__ import annotations

import hashlib

# 固定命令目录（覆盖侦察/立足/收集/横向尝试；横跨 M/T/C/I 四类可矛盾面）
_COMMANDS = [
    "whoami", "uname -a", "hostname",
    "cat /etc/passwd", "cat /etc/hosts", "cat /etc/shadow",
    "ls -la /home", "ls -la /", "ps aux", "netstat -antup | head -20",
    "cat /home/*/notes.txt", "cat /var/log/auth.log | tail -5",
    "ping db01", "ping web01", "ping nosuchhost.fake",
    "wget http://10.20.3.10/robots.txt -O /tmp/r.txt",
    "echo benchmark-marker-{i} > /tmp/m{i}.txt", "cat /tmp/m{i}.txt",
]

_HTTP_PATHS = ["/", "/admin", "/wp-login.php", "/api/users", "/.env",
               "/robots.txt", "/login", "/static/js/app.js"]


def build_attack_script(seed: str = "m2-benchmark", n_sessions: int = 3,
                        cmds_per_session: int = 8, http_requests: int = 8) -> list[dict]:
    """返回动作序列：[{step, proto, ...}]。ssh 动作带 session 分组。"""
    actions: list[dict] = []
    step = 0

    for s in range(n_sessions):
        session = f"bench-s{seed[:8]}-{s}"
        actions.append({"step": step, "proto": "ssh", "op": "connect",
                        "session": session, "dst_host": "ssh"})
        step += 1
        # 暴破 + 成功登录（脚本统一：先错一次再成功，制造 M/T 面）
        actions.append({"step": step, "proto": "ssh", "op": "auth_fail",
                        "session": session, "user": "admin",
                        "password": f"wrong-{s}"})
        step += 1
        actions.append({"step": step, "proto": "ssh", "op": "auth",
                        "session": session, "user": "root",
                        "password": _script_secret(seed, s)})
        step += 1
        for c in range(cmds_per_session):
            raw = _COMMANDS[(s * cmds_per_session + c) % len(_COMMANDS)]
            cmd = raw.replace("{i}", str(c))
            actions.append({"step": step, "proto": "ssh", "op": "exec",
                            "session": session, "cmd": cmd})
            step += 1
        actions.append({"step": step, "proto": "ssh", "op": "close",
                        "session": session})
        step += 1

    for i in range(http_requests):
        actions.append({"step": step, "proto": "http", "op": "request",
                        "session": f"bench-http-{seed[:8]}",
                        "method": "GET",
                        "path": _HTTP_PATHS[i % len(_HTTP_PATHS)],
                        "query": f"?q=benchmark{i}" if i % 3 == 0 else ""})
        step += 1

    return actions


def _script_secret(seed: str, s: int) -> str:
    """脚本内使用的登录口令（对真蜜罐是任意口令；对我们的世界与种子一致）。"""
    return "bench-" + hashlib.sha256(f"{seed}:{s}".encode()).hexdigest()[:16]
