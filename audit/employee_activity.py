"""Bounded private activity ledger for the employee workers and Dashboard."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


def record_activity(path, rows):
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS employee_activity "
                   "(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        db.executemany("INSERT INTO employee_activity(payload) VALUES (?)",
                       [(json.dumps(row, ensure_ascii=False),) for row in rows])
        db.execute("DELETE FROM employee_activity WHERE id <= "
                   "(SELECT MAX(id) - 5000 FROM employee_activity)")


def recent_activity(path):
    if not path or not Path(path).is_file():
        return []
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='employee_activity'").fetchone():
            return []
        return [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM employee_activity ORDER BY id DESC LIMIT 100"
        )]
