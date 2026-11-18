"""Export only terminal-facing synthetic facts from an authoritative DWS view."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from types import SimpleNamespace

from dws.ids import entity_uuid
from generation.templates import GroundedTemplates


def publish(world, destination, hostname):
    version = world.meta["world_version"]
    view = world.view(version)
    host_id = entity_uuid("host", hostname)
    if view.current("host", host_id) is None:
        raise ValueError("Cannot project an unknown DWS host")
    identities = sorted(view.all_current_cls("identity"), key=lambda row: row.payload["username"])
    owners = {row.entity_id: 0 if row.payload["username"] == "root" else 1000 + index
              for index, row in enumerate(identities)}
    files = {}
    for row in view.all_current_cls("data"):
        data = row.payload
        path = PurePosixPath(data["path"])
        if str(path) != data["path"] or ".." in path.parts or path.parts[:2] not in (
            ("/", "home"), ("/", "srv"), ("/", "kb"),
        ) or len(path.parts) < 3:
            continue
        uid = owners.get(data.get("owner_entity_id"), 0)
        files[str(path)] = dict(kind=data["kind"], uid=uid, deleted=data["deleted"],
                                content=data.get("content") or "")
    templates = GroundedTemplates(world)
    session = SimpleNamespace(host_entity_id=host_id)
    for path in ("/etc/hostname", "/etc/hosts", "/etc/passwd", "/etc/os-release"):
        files[path] = dict(kind="file", uid=0, deleted=False,
                           content=templates._system_file(view, session, path) + "\n")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(dict(schema=1, world_version=version, hostname=hostname, files=files), stream)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(0o644)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return version


if __name__ == "__main__":
    from business.storage import location, restore
    from dws.world import World
    from replay.engine import Engine

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--host", default="web01")
    args = parser.parse_args()
    checkpoint = location()
    if checkpoint and Path(args.out).resolve().parent == checkpoint.resolve().parent:
        parser.error("The SSH projection must use a separate directory from DWS state")
    engine = Engine(World({"meta": {"seed": "checkpoint-reader"}}))
    if restore(engine) is None:
        parser.error("Set BUSINESS_STATE_FILE to an initialized DWS checkpoint")
    print(publish(engine.world, args.out, args.host))
