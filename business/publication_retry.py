"""Bound retries across the independent queues of the DWS writer."""

from time import time


MAX_ATTEMPTS = 5


def init(db):
    db.execute("CREATE TABLE IF NOT EXISTS publication_retry ("
               "lane TEXT, job TEXT, failures INTEGER NOT NULL, retry_at INTEGER NOT NULL,"
               "PRIMARY KEY(lane,job))")


def failure(db, lane, job):
    row = db.execute("SELECT failures FROM publication_retry WHERE lane=? AND job=?", (lane, job)).fetchone()
    failures = row[0] + 1 if row else 1
    db.execute("INSERT OR REPLACE INTO publication_retry VALUES (?,?,?,?)",
               (lane, job, failures, int(time()) + 2 ** (failures - 1)))
    return failures >= MAX_ATTEMPTS


def clear(db, lane, job):
    db.execute("DELETE FROM publication_retry WHERE lane=? AND job=?", (lane, job))
