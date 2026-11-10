"""Grounded 模板库（M0，无 LLM）。铁律 K1：事实只来自 DWS。

每个 handler 返回 (status, lines, by, proposals)：
- proposals = 待提交事实 [(cls, payload), ...]，由引擎连同 timeline 一起走 CAS 提交
- by = "template"（正常）| "template_fallback"（grounding 失败兜底，SLO 降级率）
- status = ok | error | auth_fail | rejected

系统路径内容**不存 DWS**，从 identity/host 实体实时生成（契约 §2.4 content 列说明）——
同一事实只有一个来源，这是"生成层不自由发挥"的结构性保证。
"""
from __future__ import annotations

import hashlib
import shlex

from dws.ids import entity_uuid
from . import GroundingMiss

# 系统路径 -> 生成器（契约 §2.4：系统路径内容不入库）
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_PROTECTED_PREFIXES = ("/etc", "/proc", "/var/log", "/boot")


def _stable_int(text: str, mod: int) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % mod


def _fmt_mtime(hlc_str: str) -> str:
    """HLC -> "Oct 23 09:14"（月名手工映射，避免 locale 影响确定性）。"""
    seconds = int(hlc_str.split(".", 1)[0])
    days = seconds % 1_461  # 简化历：4 年周期内循环，日序确定性
    day_of_year = days % 365
    month = min(day_of_year // 30, 11)
    day = day_of_year % 30 + 1
    secs_of_day = seconds % 86_400
    return f"{_MONTHS[month]} {day:2d} {secs_of_day // 3600:02d}:{secs_of_day % 3600 // 60:02d}"


class GroundedTemplates:
    def __init__(self, world):
        self.world = world
        self._handlers = {
            "whoami": self.cmd_whoami, "id": self.cmd_id, "pwd": self.cmd_pwd,
            "cd": self.cmd_cd, "hostname": self.cmd_hostname, "uname": self.cmd_uname,
            "cat": self.cmd_cat, "ls": self.cmd_ls, "echo": self.cmd_echo,
            "touch": self.cmd_touch, "mkdir": self.cmd_mkdir, "rm": self.cmd_rm,
            "ping": self.cmd_ping, "ssh": self.cmd_ssh, "clear": self.cmd_clear,
            "exit": self.cmd_clear, "logout": self.cmd_clear,
        }

    # ------------------------------------------------------------ 入口

    def respond(self, view, session, event, argv):
        """argv 已由引擎 shlex 解析。返回 (status, lines, by, proposals)。"""
        if not argv:
            return "ok", [], "template", []
        handler = self._handlers.get(argv[0])
        if handler is None:
            return ("error", [f"bash: {argv[0]}: command not found"], "template", [])
        if session is None:
            # 未认证会话不允许执行命令（诚实响应：路由器/交换机风；M0 直接拒绝）
            return ("error", ["This account is currently not available."], "template", [])
        return handler(view, session, event, argv)

    # ------------------------------------------------------------ 公共小件

    def _now(self) -> str:
        return self.world.meta["now_hlc"]

    def _session_host(self, view, session):
        f = view.current("host", session.host_entity_id)
        if f is None:
            raise GroundingMiss("会话主机不在世界中")
        return f

    def _is_dir(self, view, session, path: str) -> bool:
        if path in ("/", "/etc", "/var", "/var/log", "/tmp", "/srv", "/home", "/root"):
            return True
        if session.user and path == f"/home/{session.user}":
            return True
        f = view.current("data", entity_uuid("data", path))
        return f is not None and f.payload["kind"] == "dir" and not f.payload["deleted"]

    def _identity_eid(self, username: str) -> str:
        return entity_uuid("identity", username)

    # ------------------------------------------------------------ 会话/信息类

    def cmd_whoami(self, view, session, event, argv):
        return "ok", [session.user or "unknown"], "template", []

    def cmd_id(self, view, session, event, argv):
        u = session.user or "unknown"
        uid = 0 if u == "root" else 1000 + _stable_int(u, 500)
        return "ok", [f"uid={uid}({u}) gid={uid}({u}) groups={uid}({u})"], "template", []

    def cmd_pwd(self, view, session, event, argv):
        return "ok", [session.cwd], "template", []

    def cmd_cd(self, view, session, event, argv):
        target = argv[1] if len(argv) > 1 else "/"
        if target == "~":
            target = f"/home/{session.user}"
        if not self._is_dir(view, session, target):
            return ("error", [f"cd: {target}: No such file or directory"], "template", [])
        session.cwd = target
        return "ok", [], "template", []

    def cmd_hostname(self, view, session, event, argv):
        return "ok", [self._session_host(view, session).payload["hostname"]], "template", []

    def cmd_clear(self, view, session, event, argv):
        return "ok", [], "template", []

    def cmd_uname(self, view, session, event, argv):
        host = self._session_host(view, session)
        hostname = host.payload["hostname"]
        k = 10 + _stable_int("kernel:" + hostname, 80)
        kernel = f"5.15.0-{k}-generic"
        if "-a" in argv or "-a" in " ".join(argv[1:]):
            line = (f"Linux {hostname} {kernel} #1 SMP Thu Jan 1 00:00:00 UTC 2026 "
                    f"x86_64 x86_64 x86_64 GNU/Linux")
        else:
            line = "Linux"
        return "ok", [line], "template", []

    # ------------------------------------------------------------ 文件读取

    def cmd_cat(self, view, session, event, argv):
        if len(argv) < 2:
            return ("error", ["cat: missing operand"], "template", [])
        lines, status = [], "ok"
        for p in argv[1:]:
            if p == "/etc/shadow":
                status = "error"
                lines.append(f"cat: {p}: Permission denied")
                continue
            try:
                text = self._file_content(view, session, p)
            except _IsDir:
                status = "error"
                lines.append(f"cat: {p}: Is a directory")
                continue
            if text is None:
                status = "error"
                lines.append(f"cat: {p}: No such file or directory")
            else:
                lines.extend(text.split("\n") if text else [])
        return status, lines, "template", []

    def _file_content(self, view, session, path: str):
        """返回文件内容字符串；不存在返回 None。系统路径由实体生成（grounding）。"""
        gen = self._system_file(view, session, path)
        if gen is not None:
            return gen
        f = view.current("data", entity_uuid("data", path))
        if f is None or f.payload["deleted"]:
            return None
        if f.payload["kind"] == "dir":
            raise _IsDir(path)
        return f.payload["content"] or ""

    def _system_file(self, view, session, path: str):
        if path == "/etc/passwd":
            idents = sorted(view.all_current_cls("identity"),
                            key=lambda f: f.payload["username"])
            lines = []
            for i, f in enumerate(idents):
                u = f.payload["username"]
                uid = 0 if u == "root" else 1000 + i
                home = "/root" if u == "root" else f"/home/{u}"
                lines.append(f"{u}:x:{uid}:{uid}::{home}:/bin/bash")
            return "\n".join(lines)
        if path == "/etc/hosts":
            lines = ["127.0.0.1\tlocalhost"]
            for f in sorted(view.all_current_cls("host"),
                            key=lambda f: f.payload["hostname"]):
                lines.append(f"{f.payload['ip']}\t{f.payload['hostname']}")
            return "\n".join(lines)
        if path == "/etc/hostname":
            return self._session_host(view, session).payload["hostname"]
        if path == "/etc/os-release":
            os_name = self._session_host(view, session).payload["os"] or "Linux"
            parts = os_name.split()
            name = parts[0] if parts else "Linux"
            version = parts[1] if len(parts) > 1 else "0"
            return (f'PRETTY_NAME="{os_name}"\nNAME="{name}"\n'
                    f'VERSION_ID="{version}"\nID_LIKE="debian"')
        if path == "/proc/version":
            hostname = self._session_host(view, session).payload["hostname"]
            k = 10 + _stable_int("kernel:" + hostname, 80)
            return (f"Linux version 5.15.0-{k}-generic "
                    f"(buildd@{hostname}) (gcc 11.4.0) #1 SMP")
        return None

    def cmd_ls(self, view, session, event, argv):
        long_mode = any(a.startswith("-") and "l" in a for a in argv[1:])
        targets = [a for a in argv[1:] if not a.startswith("-")] or [session.cwd]
        lines, status = [], "ok"
        for t in targets:
            if t == "~":
                t = f"/home/{session.user}"
            if not self._is_dir(view, session, t):
                status = "error"
                lines.append(f"ls: cannot access '{t}': No such file or directory")
                continue
            entries = self._list_dir(view, session, t)
            if long_mode:
                lines.append(f"total {len(entries)}")
                for name, kind, owner, content, mtime in entries:
                    perm = "drwxr-xr-x" if kind == "dir" else "-rw-r--r--"
                    size = len(content or "")
                    lines.append(
                        f"{perm} 1 {owner} {owner} {size:6d} {_fmt_mtime(mtime)} {name}")
            else:
                lines.append("  ".join(name for name, *_ in entries))
        return status, lines, "template", []

    def _list_dir(self, view, session, dirpath: str):
        if dirpath == "/":
            out = [("etc", "dir", "root", "", "0"), ("var", "dir", "root", "", "0"),
                   ("tmp", "dir", "root", "", "0"), ("srv", "dir", "root", "", "0"),
                   ("home", "dir", "root", "", "0")]
        elif dirpath in ("/home",):
            out = [(f.payload["username"] + "/", "dir", "root", "", "0")
                   for f in sorted(view.all_current_cls("identity"),
                                   key=lambda f: f.payload["username"])
                   if f.payload["username"] != "root"]
        else:
            out = []
            prefix = dirpath.rstrip("/") + "/"
            for f in sorted(view.all_current_cls("data"),
                            key=lambda f: f.payload["path"]):
                p = f.payload["path"]
                if f.payload["deleted"] or not p.startswith(prefix):
                    continue
                rest = p[len(prefix):]
                if not rest or "/" in rest:
                    continue
                owner = "root"
                if f.payload.get("owner_entity_id"):
                    of = view.current("identity", f.payload["owner_entity_id"])
                    if of:
                        owner = of.payload["username"]
                name = rest + ("/" if f.payload["kind"] == "dir" else "")
                out.append((name, f.payload["kind"], owner,
                            f.payload["content"], f.payload["mtime_hlc"]))
        return out

    # ------------------------------------------------------------ 文件写入（产提案，由引擎提交）

    def cmd_echo(self, view, session, event, argv):
        tokens = argv
        idx, op = None, None
        for i, t in enumerate(tokens):
            if t in (">", ">>"):
                idx, op = i, t
                break
        if idx is None:
            return "ok", [" ".join(tokens[1:])], "template", []
        if idx + 1 >= len(tokens):
            return ("error", ["bash: syntax error near unexpected token `newline'"],
                    "template", [])
        text = " ".join(tokens[1:idx]).strip("'\"")
        path = tokens[idx + 1]
        if any(path.startswith(p) for p in _PROTECTED_PREFIXES) and session.user != "root":
            return ("error", [f"bash: {path}: Permission denied"], "template", [])
        return "ok", [], "template", [("data", self._data_proposal(view, session, path,
                                                                   text, append=(op == ">>")))]

    def cmd_touch(self, view, session, event, argv):
        if len(argv) < 2:
            return ("error", ["touch: missing file operand"], "template", [])
        proposals = [("data", self._data_proposal(view, session, p, None, append=False))
                     for p in argv[1:]]
        return "ok", [], "template", proposals

    def cmd_mkdir(self, view, session, event, argv):
        paths = [a for a in argv[1:] if not a.startswith("-")]
        if not paths:
            return ("error", ["mkdir: missing operand"], "template", [])
        lines, status, proposals = [], "ok", []
        for p in paths:
            if self._is_dir(view, session, p) or view.current(
                    "data", entity_uuid("data", p)) is not None:
                status = "error"
                lines.append(f"mkdir: cannot create directory '{p}': File exists")
                continue
            proposals.append(("data", self._data_proposal(view, session, p, None,
                                                          append=False, kind="dir")))
        return status, lines, "template", proposals

    def cmd_rm(self, view, session, event, argv):
        paths = [a for a in argv[1:] if not a.startswith("-")]
        if not paths:
            return ("error", ["rm: missing operand"], "template", [])
        lines, status, proposals = [], "ok", []
        for p in paths:
            cur = view.current("data", entity_uuid("data", p))
            if cur is None or cur.payload["deleted"]:
                status = "error"
                lines.append(f"rm: cannot remove '{p}': No such file or directory")
                continue
            payload = dict(cur.payload)
            payload["deleted"] = True
            payload["mtime_hlc"] = self._now()
            proposals.append(("data", payload))
        return status, lines, "template", proposals

    def _data_proposal(self, view, session, path, content, append, kind="file"):
        cur = view.current("data", entity_uuid("data", path))
        now = self._now()
        if cur is not None and not cur.payload["deleted"]:
            old = cur.payload["content"] or ""
            new_content = (old + content) if (append and content is not None) else content
            return {"path": path,
                    "owner_entity_id": cur.payload["owner_entity_id"],
                    "ctime_hlc": cur.payload["ctime_hlc"],
                    "mtime_hlc": now,
                    "kind": cur.payload["kind"],
                    "content": new_content,
                    "canary": cur.payload["canary"],
                    "deleted": False}
        return {"path": path,
                "owner_entity_id": self._identity_eid(session.user) if session.user else None,
                "ctime_hlc": now,
                "mtime_hlc": now,
                "kind": kind,
                "content": content,
                "canary": False,
                "deleted": False}

    # ------------------------------------------------------------ 网络（grounded 于 relation）

    def cmd_ping(self, view, session, event, argv):
        if len(argv) < 2:
            return ("error", ["ping: usage error: Destination address required"],
                    "template", [])
        target = argv[1]
        tf = view.current("host", entity_uuid("host", target))
        if tf is None:
            return ("error", [f"ping: {target}: Name or service not found"], "template", [])
        src = session.host_entity_id
        reachable = any(
            r.payload["from_entity_id"] == src
            and r.payload["to_entity_id"] == tf.entity_id
            for r in view.all_current_cls("relation"))
        ip = tf.payload["ip"]
        if not reachable:
            return ("error",
                    [f"PING {target} ({ip}) 56(84) bytes of data.",
                     f"From {self._session_host(view, session).payload['ip']} icmp_seq=1 Destination Host Unreachable",
                     f"--- {target} ping statistics ---",
                     "3 packets transmitted, 0 received, 100% packet loss"],
                    "template", [])
        base = 5 + _stable_int("rtt:" + target, 40)
        lines = [f"PING {target} ({ip}) 56(84) bytes of data."]
        for seq in range(1, 4):
            t = base + seq
            lines.append(f"64 bytes from {target} ({ip}): icmp_seq={seq} ttl=64 "
                         f"time={t // 10}.{t % 10} ms")
        lines.append(f"--- {target} ping statistics ---")
        lines.append("3 packets transmitted, 3 received, 0% packet loss")
        return "ok", lines, "template", []

    def cmd_ssh(self, view, session, event, argv):
        if len(argv) < 2:
            return ("error", ["usage: ssh user@host"], "template", [])
        arg = argv[1]
        user, _, host = arg.partition("@")
        host = host or user
        user = user if "@" in arg else session.user
        tf = view.current("host", entity_uuid("host", host))
        reachable = False
        if tf is not None:
            svc = [s["port"] for s in tf.payload["services"]]
            reachable = (22 in svc and any(
                r.payload["from_entity_id"] == session.host_entity_id
                and r.payload["to_entity_id"] == tf.entity_id
                for r in view.all_current_cls("relation")))
        if not reachable:
            return ("error",
                    [f"ssh: connect to host {host} port 22: Connection refused"],
                    "template", [])
        return ("error",
                [f"Password: ", "Permission denied (publickey,password).",
                 f"Connection closed by {tf.payload['ip']} port 22"],
                "template", [])


class _IsDir(Exception):
    """cat 一个目录（引擎转成 'Is a directory' 错误行）。"""

    def __init__(self, path):
        super().__init__(path)
        self.path = path
