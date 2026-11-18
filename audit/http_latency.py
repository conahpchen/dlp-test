"""Bounded request-duration samples shared with the loopback dashboard."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from time import perf_counter_ns

from starlette.concurrency import run_in_threadpool


def open_samples(path):
    return sqlite3.connect(path, timeout=2)


def record(path, status, duration_ns):
    with closing(open_samples(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS http_samples "
                   "(id INTEGER PRIMARY KEY, status INTEGER, duration_ns INTEGER)")
        db.execute("INSERT INTO http_samples(status,duration_ns) VALUES (?,?)",
                   (status, duration_ns))
        db.execute("DELETE FROM http_samples WHERE id <= (SELECT MAX(id)-2048 FROM http_samples)")


def samples():
    path = os.environ.get("HONEYNET_LATENCY_FILE")
    if not path or not Path(path).is_file():
        return []
    with closing(open_samples(path)) as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='http_samples'").fetchone()
        return db.execute("SELECT status,duration_ns FROM http_samples ORDER BY id").fetchall() if table else []


def install(app):
    @app.middleware("http")
    async def sample_request(request, call_next):
        path = os.environ.get("HONEYNET_LATENCY_FILE")
        if not path:
            return await call_next(request)
        start = perf_counter_ns()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            try:
                await run_in_threadpool(record, path, status, perf_counter_ns() - start)
            except sqlite3.Error:
                # Sampling failures must not take down the observed service.
                pass
