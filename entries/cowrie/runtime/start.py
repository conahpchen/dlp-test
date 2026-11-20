"""Cowrie 3.1.7 adapter loaded in the sensor process only."""

import hashlib
import json
import os
import re
import runpy
import sqlite3
import struct
from contextlib import closing
from contextvars import ContextVar
from time import time
import sys
from pathlib import Path


def read_projection(projection):
    path = Path(projection)
    if path.suffix != ".sqlite":
        return json.loads(path.read_text(encoding="utf-8"))
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        row = db.execute("SELECT payload FROM active_snapshot WHERE id=1").fetchone()
        if row is None:
            raise ValueError("No committed DWS projection")
        return json.loads(row[0])


def install(projection):
    from cowrie.shell import fs
    from cowrie.shell.protocol import HoneyPotBaseProtocol
    from cowrie.ssh.session import HoneyPotSSHSession
    from twisted.conch.ssh.common import getNS

    selected = ContextVar("ssh_dws_binding", default=None)
    request_env = HoneyPotSSHSession.request_env

    def binding_env(channel, data):
        try:
            name, rest = getNS(data)
            if name != b"HONEYNET_BINDING":
                return request_env(channel, data)
            value, trailing = getNS(rest)
            if trailing or channel.client is not None or getattr(channel, "dws_binding", None) is not None:
                channel.dws_binding = False
                return 0
            if not re.fullmatch(rb"[a-f0-9]{24}", value):
                channel.dws_binding = False
                return 0
            identifier = hashlib.sha256(value).hexdigest()
            path = Path(projection).parent / "bindings" / (identifier + ".json")
            snapshot = read_projection(path)
            if (snapshot["expires_at"] <= time() or snapshot["schema"] != 1
                    or snapshot["hostname"] != os.environ["COWRIE_HONEYPOT_HOSTNAME"]):
                channel.dws_binding = False
                return 0
            channel.dws_binding = snapshot
            return 1
        except (TypeError, ValueError, KeyError, OSError, struct.error):
            channel.dws_binding = False
            return 0

    def with_binding(original):
        def request(channel, data):
            snapshot = getattr(channel, "dws_binding", None)
            if snapshot is False or (snapshot and snapshot["expires_at"] <= time()):
                return 0
            server = getattr(channel.avatar, "server", None)
            filesystem = getattr(server, "fs", None)
            rebind = snapshot and filesystem and filesystem.dws_world_version != snapshot["world_version"]
            if rebind and getattr(server, "_dws_started", False):
                return 0
            token = selected.set(snapshot)
            try:
                if rebind:
                    # Cowrie can initialize its filesystem during PTY setup
                    # before the client sends environment requests.
                    initialize_with_dws(filesystem, filesystem.arch, filesystem.home)
                result = original(channel, data)
                if result and server is not None:
                    server._dws_started = True
                return result
            finally:
                selected.reset(token)
        return request

    HoneyPotSSHSession.request_env = binding_env
    HoneyPotSSHSession.request_shell = with_binding(HoneyPotSSHSession.request_shell)
    HoneyPotSSHSession.request_exec = with_binding(HoneyPotSSHSession.request_exec)

    if os.environ.get("RESEARCH_AUTOMATIC_DECOYS") == "1":
        original_call = HoneyPotBaseProtocol.call_command

        def call_command(protocol, pp, cmd, *args):
            if not getattr(protocol, "_dws_decoy_delivered", False):
                try:
                    session = protocol.getProtoTransport().transportId
                    key = hashlib.sha256(f"ssh:{session}".encode()).hexdigest()
                    path = Path(projection).parent / "session-decoys" / (key + ".json")
                    with path.open("rb") as stream:
                        decoy = json.loads(stream.read(4096))
                    title, note = decoy["title"], decoy["note"]
                    destination = "/etc/service-reference.conf"
                    filesystem = protocol.fs
                    if (decoy["expires"] > time() and re.fullmatch(r"[a-z][a-z0-9_-]{2,47}", title)
                            and len(note) <= 300 and note.isprintable()
                            and not filesystem.exists(destination)):
                        content = f"# {note}\n[service]\nname={title}\nmode=read_only\n".encode()
                        filesystem.mkfile(destination, 0, 0, len(content), 0o100444)
                        filesystem.getfile(destination)[fs.A_CONTENTS] = content
                        protocol._dws_decoy_delivered = True
                except (AttributeError, OSError, TypeError, ValueError, KeyError):
                    pass
            return original_call(protocol, pp, cmd, *args)

        HoneyPotBaseProtocol.call_command = call_command

    initialize = fs.HoneyPotFilesystem.__init__

    def parent_dirs(filesystem, path):
        if path == "/":
            return
        parent_dirs(filesystem, str(Path(path).parent))
        if not filesystem.exists(path):
            filesystem.mkdir(path, 0, 0, 0, 0o40755)
        if not filesystem.isdir(path) or filesystem.realpath(path) != path:
            raise ValueError("Invalid projected directory")

    def initialize_with_dws(filesystem, *args, **kwargs):
        snapshot = selected.get() or read_projection(projection)
        if snapshot["schema"] != 1 or snapshot["hostname"] != os.environ["COWRIE_HONEYPOT_HOSTNAME"]:
            raise ValueError("Cowrie hostname and DWS projection must agree")
        initialize(filesystem, *args, **kwargs)
        for path, item in sorted(snapshot["files"].items(), key=lambda entry: entry[0].count("/")):
            if item["deleted"]:
                if filesystem.isfile(path):
                    filesystem.remove(path)
                elif filesystem.isdir(path):
                    filesystem.rmdir(path)
                continue
            uid = item["uid"]
            if item["kind"] == "dir":
                parent_dirs(filesystem, path)
                filesystem.chown(path, uid, uid)
                continue
            parent_dirs(filesystem, str(Path(path).parent))
            data = item["content"].encode("utf-8")
            mode = 0o100644 if path.startswith("/etc/") else 0o100640
            filesystem.mkfile(path, uid, uid, len(data), mode)
            filesystem.getfile(path)[fs.A_CONTENTS] = data
        filesystem.dws_world_version = snapshot["world_version"]

    fs.HoneyPotFilesystem.__init__ = initialize_with_dws


if __name__ == "__main__":
    projection = Path(os.environ["HONEYNET_COWRIE_PROJECTION"])
    if not projection.is_file():
        raise SystemExit("The DWS SSH projection is unavailable")
    install(projection)
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")
