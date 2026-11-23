"""Export only terminal-facing synthetic facts from an authoritative DWS view."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from types import SimpleNamespace

from dws.ids import entity_uuid
from generation.templates import GroundedTemplates


def prepare(world, hostname, *, version=None, branch_version=None):
    current = world.meta["world_version"]
    version = current if version is None else version
    if not 1 <= version <= current:
        raise ValueError("DWS projection version is unavailable")
    if branch_version is not None and branch_version not in world.private_versions:
        raise ValueError("DWS branch projection version is unavailable")
    view = world.view(version, (branch_version,) if branch_version is not None else ())
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
    snapshot = dict(schema=1, world_version=version, hostname=hostname, files=files)
    if branch_version is not None:
        snapshot["branch_version"] = branch_version
    return snapshot


def publish(world, destination, hostname):
    snapshot = prepare(world, hostname)
    destination = Path(destination)
    if destination.suffix == ".sqlite":
        raise ValueError("SQLite projections must be written with their DWS checkpoint")
    write_snapshot(snapshot, destination)
    return snapshot["world_version"]


def write_snapshot(snapshot, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(snapshot, stream)
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
