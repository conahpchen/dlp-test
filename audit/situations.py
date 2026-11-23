"""Passive situation reports from retained observations."""

import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

WINDOW = 3600
CONFIRMED_USE = (
    "COALESCE((SELECT associated FROM canary_use_revisions v "
    "WHERE v.use_id=u.id ORDER BY revision DESC LIMIT 1),1)=1"
)


def _stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def _read(path):
    if not path or not Path(path).is_file():
        yield None
        return
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        yield db


def _has(db, table):
    return db is not None and db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def publish_reports(now=None):
    destination = os.environ.get("HONEYNET_SITUATION_REPORT_FILE")
    if not destination:
        return 0
    now = int(time.time()) if now is None else now
    start, stop = _stamp(now - WINDOW), _stamp(now + 1)
    snapshot = {"generated_at": _stamp(now), "window_start": start,
                "alerts": None, "canary_matches": None}
    with _read(os.environ.get("HONEYNET_ALERTS_FILE")) as db:
        if _has(db, "alerts"):
            snapshot["alerts"] = [
                {"entry": source, "kind": kind, "count": count}
                for source, kind, count in db.execute(
                    "SELECT source,kind,COUNT(*) FROM alerts WHERE recorded_at>=? AND recorded_at<? "
                    "GROUP BY source,kind ORDER BY source,kind", (start, stop),
                )
            ]
    triggers = []
    with _read(os.environ.get("RESEARCH_STATE_FILE")) as db:
        if all(_has(db, table) for table in (
            "canary_credentials", "lure_response_receipts", "lure_uses", "canary_use_revisions",
        )):
            snapshot["canary_matches"] = dict(db.execute(
                "SELECT u.entry,COUNT(*) FROM lure_uses u "
                "JOIN lure_response_receipts r ON r.token_hash=u.token_hash "
                f"WHERE u.recorded_at>=? AND u.recorded_at<? AND {CONFIRMED_USE} GROUP BY u.entry",
                (start, stop),
            ))
            directed = _has(db, "canary_handoffs")
            root = "COALESCE(h.root_id,c.credential_id)" if directed else "c.credential_id"
            join = "LEFT JOIN canary_handoffs h ON h.id=c.credential_id " if directed else ""
            triggers = db.execute(
                f"SELECT c.deployment_id,{root},COUNT(DISTINCT u.entry) AS targets "
                "FROM canary_credentials c JOIN lure_response_receipts r ON r.token_hash=c.credential_id "
                f"JOIN lure_uses u ON u.token_hash=c.credential_id {join}"
                f"WHERE u.recorded_at>=? AND u.recorded_at<? AND {CONFIRMED_USE} "
                f"GROUP BY c.deployment_id,{root} HAVING targets>=2 "
                "ORDER BY MAX(u.id) DESC LIMIT 100", (start, stop),
            ).fetchall()
    candidates = [(f"period:{now // WINDOW}", dict(snapshot, kind="hourly"))]
    candidates.extend((f"cross:{key}", dict(snapshot, kind="cross_protocol", run=run, target_count=targets))
                      for run, key, targets in triggers)
    with closing(sqlite3.connect(destination, timeout=4)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS situation_reports (id INTEGER PRIMARY KEY, "
                   "report_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)")
        inserted = db.executemany("INSERT OR IGNORE INTO situation_reports(report_key,payload) VALUES (?,?)",
                                  [(key, json.dumps(data, ensure_ascii=False)) for key, data in candidates]).rowcount
        db.execute("DELETE FROM situation_reports WHERE id <= "
                   "(SELECT MAX(id) - 500 FROM situation_reports)")
        return inserted


def list_reports():
    with _read(os.environ.get("HONEYNET_SITUATION_REPORT_FILE")) as db:
        if not _has(db, "situation_reports"):
            return []
        if not _has(db, "situation_analysis"):
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM situation_reports ORDER BY id DESC LIMIT 50")]
        return [dict(json.loads(report), analysis=json.loads(analysis)) if analysis else json.loads(report)
                for report, analysis in db.execute(
                    "SELECT r.payload,a.payload FROM situation_reports r "
                    "LEFT JOIN situation_analysis a ON a.report_id=r.id ORDER BY r.id DESC LIMIT 50")]
