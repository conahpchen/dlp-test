"""Store, review and apply narrowly scoped deception-evidence actions."""

import sqlite3
from time import time


def _result(db, sql, args=()):
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(sql, args)]
    finally:
        db.row_factory = None


def _get(db, identifier):
    rows = _result(db, "SELECT * FROM response_requests WHERE id=?", (identifier,))
    if not rows:
        raise KeyError(identifier)
    return rows[0]


def list_requests(lab):
    with lab.connect() as db:
        return _result(db, "SELECT * FROM response_requests ORDER BY id DESC LIMIT 100")


def _inspect(lab, db, run, kind, target):
    if kind == "revoke":
        credential = lab._canary(db, target)
    else:
        use = lab._canary_use(db, int(target))
        credential = lab._canary(db, use["credential_id"])
    if credential is None or credential["deployment_id"] != run:
        raise KeyError(target)
    if kind == "revoke":
        if lab._canary_status(credential, int(time())) != "active":
            raise ValueError("Canary 已失效")
        return None
    if use["associated"] == (kind == "restore"):
        raise ValueError("关联已经是请求中的状态")
    return use["revision"]


def submit(lab, run, kind, target, reason):
    with lab.connect() as db:
        revision = _inspect(lab, db, run, kind, target)
        pending = db.execute("SELECT count(*) FROM response_requests WHERE status='pending'").fetchone()[0]
        if pending >= 100:
            raise ValueError("待审批处置已满")
        try:
            result = db.execute(
                "INSERT INTO response_requests(run,kind,target,revision,reason,requested_by,requested_at) "
                "VALUES(?,?,?,?,?,'operator',?)", (run, kind, target, revision, reason, int(time())),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("此对象已有待审批的同类请求") from exc
        return _get(db, result.lastrowid)


def decide(lab, identifier, approved, reason):
    with lab.connect() as db:
        request = _get(db, identifier)
        if request["status"] != "pending":
            raise ValueError("处置已审核")
        status = "rejected"
        if approved:
            kind, target = request["kind"], request["target"]
            try:
                revision = _inspect(lab, db, request["run"], kind, target)
                if revision != request["revision"]:
                    raise ValueError("关联版本已经变化")
            except (KeyError, ValueError):
                status = "conflict"
            else:
                if kind == "revoke":
                    lab._revoke_canary(db, target)
                else:
                    lab._revise_canary_use(db, int(target), revision, kind == "restore", request["reason"])
                status = "applied"
        db.execute("UPDATE response_requests SET status=?,reviewed_by='reviewer',reviewed_at=?,review_reason=? WHERE id=?",
                   (status, int(time()), reason, identifier))
        return _get(db, identifier)
