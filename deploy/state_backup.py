"""Back up or restore a stopped reference deployment into a fresh directory."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from business.atomic_state import read


STATE_FILES = frozenset({
    "world.sqlite", "research.sqlite", "proposals.sqlite", "flow-evidence.sqlite",
    "alerts.sqlite", "http.sqlite", "employees.sqlite", "situations.sqlite", "model-budget.sqlite",
})
REQUIRED = frozenset({"state/world.sqlite", "state/research.sqlite"})
COWRIE_DIRS = ("bindings", "handoffs", "session-decoys")


def _safe_files(state, projection=None):
    sources = {}
    for name in sorted(STATE_FILES):
        candidate = state / name
        if candidate.is_file():
            sources["state/" + name] = candidate
    if projection is not None:
        sources["cowrie/world.sqlite"] = projection
        for directory in COWRIE_DIRS:
            folder = projection.parent / directory
            if folder.is_symlink():
                raise ValueError("Cowrie projection contains a symlink")
            for file in sorted(folder.glob("*.json")):
                if file.is_file():
                    sources[f"cowrie/{directory}/{file.name}"] = file
    if not REQUIRED <= sources.keys() or any(file.is_symlink() for file in sources.values()):
        raise ValueError("State files are missing or are not regular files")
    return sources


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _manifest(bundle):
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Invalid backup manifest")
    projection = bundle / "cowrie" / "world.sqlite"
    actual = _safe_files(bundle / "state", projection if projection.exists() else None)
    if not REQUIRED <= actual.keys() or actual.keys() != manifest["files"].keys():
        raise ValueError("Backup manifest does not match its files")
    if any(file.resolve().parent != file.parent.absolute() or _digest(file) != manifest["files"][name]
           for name, file in actual.items()):
        raise ValueError("Backup integrity validation failed")
    if _version(bundle) != manifest.get("world_version"):
        raise ValueError("Backup world version does not match its manifest")
    return manifest


def _version(bundle):
    private = read(bundle / "state" / "world.sqlite")["meta"]["world_version"]
    projection = bundle / "cowrie" / "world.sqlite"
    if projection.exists() and read(projection)["world_version"] != private:
        raise ValueError("DWS and Cowrie snapshots are from different versions")
    return private


def _copy_db(source, destination):
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as existing, closing(
        sqlite3.connect(destination)
    ) as copied:
        existing.backup(copied)
        if copied.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite backup integrity validation failed")


def snapshot(state, projection, destination):
    """Capture the DWS, research ledger and configured sensors after write processes stop."""
    state, destination = Path(state).resolve(), Path(destination).absolute()
    projection = Path(projection).absolute() if projection else None
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    sources = _safe_files(state, projection)
    with TemporaryDirectory(dir=destination.parent, prefix=".dlp-backup-") as temporary:
        staged = Path(temporary) / "bundle"
        staged.mkdir(mode=0o700)
        files = {}
        for name, source in sources.items():
            output = staged / name
            output.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == ".sqlite":
                _copy_db(source, output)
            else:
                shutil.copyfile(source, output)
            output.chmod(0o600)
            files[name] = _digest(output)
        manifest = {"schema": 1, "world_version": _version(staged), "files": files}
        (staged / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        staged.rename(destination)
        return manifest


def restore(bundle, destination):
    """Recover to a fresh state directory and stand-alone Cowrie projection."""
    bundle, destination = Path(bundle).resolve(), Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    manifest = _manifest(bundle)
    with TemporaryDirectory(dir=destination.parent, prefix=".dlp-restore-") as temporary:
        staged = Path(temporary) / "restored"
        staged.mkdir(mode=0o700)
        for name in manifest["files"]:
            output = staged / name
            cowrie = name.startswith("cowrie/")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.parent.chmod(0o755 if cowrie else 0o700)
            shutil.copyfile(bundle / name, output)
            output.chmod(0o644 if cowrie else 0o600)
        if (staged / "cowrie").is_dir():
            (staged / "cowrie").chmod(0o755)
        for name, expected in manifest["files"].items():
            if _digest(staged / name) != expected:
                raise ValueError("Restored data differs from the verified backup")
        staged.rename(destination)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("snapshot", help="Stop write processes before capturing")
    capture.add_argument("--state", type=Path, required=True)
    capture.add_argument("--cowrie", type=Path)
    capture.add_argument("--destination", type=Path, required=True)
    recovery = commands.add_parser("restore", help="Restore into a fresh directory")
    recovery.add_argument("--bundle", type=Path, required=True)
    recovery.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "snapshot":
        result = snapshot(args.state, args.cowrie, args.destination)
    else:
        result = restore(args.bundle, args.destination)
    print(json.dumps({"world_version": result["world_version"], "files_verified": len(result["files"])}))


if __name__ == "__main__":
    main()
