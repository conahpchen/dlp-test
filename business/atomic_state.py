"""Local checkpoint and SSH projection committed in a single SQLite transaction."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


def read(path, *, recover=False):
    path = Path(path)
    if path.suffix != ".sqlite":
        return json.loads(path.read_text(encoding="utf-8"))
    mode = "rw" if recover else "ro"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True, timeout=5)) as db:
        if recover and not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='active_snapshot'"
        ).fetchone():
            return None
        row = db.execute("SELECT payload FROM active_snapshot WHERE id=1").fetchone()
        if row is None:
            raise ValueError("No committed DWS snapshot")
        return json.loads(row[0])


def _store(db, schema, payload):
    db.execute(f"CREATE TABLE IF NOT EXISTS {schema}.active_snapshot "
               "(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
    db.execute(f"INSERT OR REPLACE INTO {schema}.active_snapshot VALUES (1,?)", (payload,))


def commit(checkpoint, state, projection=None, snapshot=None):
    checkpoint = Path(checkpoint).resolve()
    if projection is not None:
        projection = Path(projection).resolve()
        if projection.suffix != ".sqlite":
            raise ValueError("An atomic checkpoint requires a SQLite SSH projection")
        projection.parent.mkdir(parents=True, exist_ok=True)
        if checkpoint.parent.stat().st_dev != projection.parent.stat().st_dev:
            raise ValueError("Atomic checkpoint and SSH projection must be on the same local filesystem")
    private_payload = json.dumps(state, ensure_ascii=False)
    public_payload = json.dumps(snapshot, ensure_ascii=False) if projection is not None else None
    with closing(sqlite3.connect(checkpoint, timeout=5)) as db:
        checkpoint.chmod(0o600)
        if projection is not None:
            db.execute("ATTACH DATABASE ? AS ssh", (str(projection),))
            # The directory is dedicated to terminal-safe files. UID 999 reads
            # the exported database through a read-only mount.
            projection.chmod(0o644)
        for schema in ("main", "ssh") if projection is not None else ("main",):
            mode = db.execute(f"PRAGMA {schema}.journal_mode=DELETE").fetchone()[0]
            if mode.lower() != "delete":
                raise RuntimeError("Atomic publication requires rollback journals")
            db.execute(f"PRAGMA {schema}.synchronous=FULL")
        with db:
            db.execute("BEGIN IMMEDIATE")
            _store(db, "main", private_payload)
            if projection is not None:
                _store(db, "ssh", public_payload)
