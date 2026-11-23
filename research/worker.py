"""Durable coalesced adaptation work. Only the model control process consumes jobs."""

import asyncio
import json
import logging
import os
import secrets
from time import time

from research.controller import adapt


MAX_PENDING = 128
LEASE_SECONDS = 240


def automatic_enabled():
    return os.environ.get("RESEARCH_AUTOMATIC_DECOYS") == "1"


def enabled():
    return automatic_enabled() or os.environ.get("RESEARCH_ASYNC_ADAPTATION") == "1"


def enqueue(lab, run, session):
    with lab.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS adaptation_jobs ("
                   "run TEXT NOT NULL, session TEXT NOT NULL, lease TEXT, leased_until INTEGER, "
                   "revision INTEGER NOT NULL DEFAULT 1, "
                   "PRIMARY KEY(run,session))")
        if db.execute("UPDATE adaptation_jobs SET revision=revision+1 WHERE run=? AND session=?",
                      (run, session)).rowcount:
            return True
        if db.execute("SELECT count(*) FROM adaptation_jobs").fetchone()[0] >= MAX_PENDING:
            return False
        db.execute("INSERT INTO adaptation_jobs(run,session) VALUES (?,?)", (run, session))
        return True


def process_one(lab, model):
    now = int(time())
    lease = secrets.token_hex(16)
    with lab.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='adaptation_jobs'").fetchone():
            return False
        row = db.execute("SELECT run,session,revision FROM adaptation_jobs "
                         "WHERE lease IS NULL OR leased_until<=? ORDER BY rowid LIMIT 1",
                         (now,)).fetchone()
        if row is None:
            return False
        run, session, revision = row
        db.execute("UPDATE adaptation_jobs SET lease=?,leased_until=? WHERE run=? AND session=?",
                   (lease, now + LEASE_SECONDS, run, session))
    result = adapt(lab, run, session, model, interval=1, retry_pending=True)
    with lab.connect() as db:
        if result["status"] == "budget_exhausted" and db.execute(
            "SELECT 1 FROM adaptation_jobs WHERE run=? AND session=? AND lease=? AND revision=?",
            (run, session, lease, revision),
        ).fetchone():
            db.execute("INSERT INTO decisions(run,session,payload) VALUES (?,?,?)",
                       (run, session, json.dumps({"session": session, "status": "budget_exhausted"})))
        db.execute("DELETE FROM adaptation_jobs WHERE run=? AND session=? AND lease=? AND revision=?",
                   (run, session, lease, revision))
        db.execute("UPDATE adaptation_jobs SET lease=NULL,leased_until=NULL "
                   "WHERE run=? AND session=? AND lease=?", (run, session, lease))
    return True


def result_status(lab, run, session):
    with lab.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='adaptation_jobs'").fetchone():
            return "missing"
        if db.execute("SELECT 1 FROM adaptation_jobs WHERE run=? AND session=?",
                      (run, session)).fetchone():
            return "pending"
        result = db.execute("SELECT payload FROM decisions WHERE run=? AND session=? "
                            "ORDER BY id DESC LIMIT 1", (run, session)).fetchone()
        status = json.loads(result[0])["status"] if result is not None else "missing"
        return "pending" if status == "publishing" else status


async def consume(lab_provider, stop):
    from research.model import Model

    from research.protocol_decoys import process_one as process_protocol

    def cycle():
        lab = lab_provider()
        model = Model()
        if automatic_enabled():
            process_protocol(lab, model)
        process_one(lab, model)

    while not stop.is_set():
        try:
            await asyncio.to_thread(cycle)
        except Exception as exc:
            logging.getLogger(__name__).warning("Model queue unavailable (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
