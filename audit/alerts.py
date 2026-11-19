"""Recent connection and authentication observations shared by the listeners."""

import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

LIMIT = 5000
SCHEMA = """CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, event_key TEXT UNIQUE
)"""


def record(source, kind, event_key=None):
    path = os.environ.get("HONEYNET_ALERTS_FILE")
    if not path:
        return
    try:
        with closing(sqlite3.connect(path, timeout=.25)) as db, db:
            db.execute(SCHEMA)
            db.execute("INSERT OR IGNORE INTO alerts(source, kind, event_key) VALUES (?,?,?)",
                       (source, kind, event_key))
            latest = db.execute("SELECT MAX(id) FROM alerts").fetchone()[0]
            db.execute("DELETE FROM alerts WHERE id <= ?", (latest - LIMIT,))
    except sqlite3.Error:
        logging.getLogger(__name__).warning("Alert observation could not be stored")


def read_alerts(source=None):
    rows = []
    collected = False
    path = os.environ.get("HONEYNET_ALERTS_FILE")
    if path and Path(path).is_file():
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='alerts'").fetchone():
                collected = True
                rows = db.execute(
                    "SELECT source, kind, recorded_at FROM alerts "
                    "WHERE (? IS NULL OR source=?) ORDER BY id DESC LIMIT 100", (source, source),
                ).fetchall()
    research = os.environ.get("RESEARCH_STATE_FILE")
    if research and Path(research).is_file():
        with closing(sqlite3.connect(Path(research).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='lure_uses'").fetchone():
                collected = True
                rows += db.execute(
                    "SELECT entry, 'canary', recorded_at FROM lure_uses "
                    "WHERE (? IS NULL OR entry=?) ORDER BY id DESC LIMIT 100", (source, source),
                ).fetchall()
    rows.sort(key=lambda row: row[2], reverse=True)
    return {"has_data_source": collected, "alerts": [
        {"source": sensor, "kind": kind, "recorded_at": timestamp}
        for sensor, kind, timestamp in rows[:100]
    ]}
