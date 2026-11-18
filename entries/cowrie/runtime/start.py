"""Cowrie 3.1.7 adapter loaded in the sensor process only."""

import json
import os
import runpy
import sqlite3
from contextlib import closing
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
        snapshot = read_projection(projection)
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
