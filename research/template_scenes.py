"""Share template generation requests between the Dashboard and the model control process."""

import asyncio
import logging
import secrets
import sqlite3
from threading import Event, Thread
from time import time

from pydantic import ValidationError

from research.generation import VerificationRequired
from research.model import MODEL_FAILURES, Model, failure_code
from research.template_grounding import TemplateGroundingError


MAX_PENDING = 32
HEARTBEAT_SECONDS = 15
LEASE_SECONDS = 75
JOB_STATE = ("SELECT id,status,error,stage,started_at,updated_at FROM template_scene_jobs "
             "WHERE template_id=? AND revision=?")
SCENE_ERRORS = {
    "Independent model review rejected scene": "scene_review_rejected",
    "Use structured DWS claims for infrastructure facts": "scene_dws_validation",
    "Use a matching DWS claim for infrastructure prose": "scene_dws_validation",
    "Use a matching DWS claim for infrastructure fields": "scene_dws_validation",
    "DWS claim contradicts the baseline": "scene_dws_validation",
    "Unknown DWS claim slot": "scene_dws_validation",
    "Unverifiable source quote": "scene_dws_validation",
    "Invalid DWS entity anchors": "scene_dws_anchors",
    "Template draft attempted to supply server-owned DWS metadata": "scene_dws_validation",
    "DWS anchors must include an existing relationship": "scene_dws_anchors",
    "DWS snapshot contains no usable anchor relationship": "world_no_relationships",
    "Dangling scene link": "scene_navigation",
    "Unreachable scene node": "scene_navigation",
    "Scene graph edge or node degree limit exceeded": "scene_navigation",
}


def _state(row):
    if row is None:
        return None
    identifier, status, error, stage, started, updated = row
    if status == "queued":
        started = updated
    end = int(time()) if status in ("queued", "running") else updated
    elapsed = max(0, end - started) if end is not None and started is not None else None
    return {"status": status, "job_id": identifier, "run_id": identifier if status == "ready" else None,
            "error": error, "stage": stage, "elapsed_seconds": elapsed}


def status(lab, template_id, revision):
    with lab.connect(write=False) as db:
        return _state(db.execute(JOB_STATE, (template_id, revision)).fetchone())


def enqueue(lab, template_id, revision):
    lab.template(template_id, revision)
    with lab.connect() as db:
        current = db.execute(JOB_STATE, (template_id, revision)).fetchone()
        if current and current[1] in ("queued", "running"):
            return _state(current)
        if db.execute("SELECT count(*) FROM template_scene_jobs WHERE status IN ('queued','running')").fetchone()[0] >= MAX_PENDING:
            raise OverflowError("Template generation queue is full")
        identifier = secrets.token_hex(16)
        now = int(time())
        db.execute("INSERT OR REPLACE INTO template_scene_jobs(template_id,revision,id,status,updated_at) "
                   "VALUES (?,?,?,'queued',?)", (template_id, revision, identifier, now))
        return _state((identifier, "queued", None, None, None, now))


def _generate(lab, world_provider, model, scene_job, template_id, revision):
    try:
        world = world_provider()
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return "world_unavailable"
    try:
        template = lab.template(template_id, revision)
        variant = template["variant"]
        lab.create(world, variant, model, template["goal"], template=template, scene_job=scene_job,
                   require_verification=variant == "agent_note")
    except MODEL_FAILURES as exc:
        return failure_code(exc)
    except TemplateGroundingError as exc:
        return exc.code
    except VerificationRequired:
        return "verification_missing"
    except ValidationError:
        return "scene_schema"
    except ValueError as exc:
        return SCENE_ERRORS.get(str(exc), "scene_validation")
    except sqlite3.Error:
        return "scene_storage"
    except Exception as exc:
        logging.getLogger(__name__).warning("Template scene generation failed (%s)", type(exc).__name__)
        return "generation_failed"
    return None


def _renew_lease(lab, job):
    with lab.connect(timeout=2) as db:
        return db.execute("UPDATE template_scene_jobs SET leased_until=? "
                          "WHERE id=? AND lease=? AND status='running'",
                          (int(time()) + LEASE_SECONDS, *job)).rowcount


def _keep_lease(lab, job, finished):
    while not finished.wait(HEARTBEAT_SECONDS):
        try:
            if not _renew_lease(lab, job):
                return
        except sqlite3.Error:
            logging.getLogger(__name__).warning("Template generation heartbeat could not be recorded")


def _progress(lab, job, stage):
    try:
        with lab.connect(timeout=1) as db:
            db.execute("UPDATE template_scene_jobs SET stage=?,updated_at=? WHERE id=? AND lease=? AND status='running'",
                       (stage, int(time()), *job))
    except sqlite3.Error:
        logging.getLogger(__name__).warning("Template generation progress could not be recorded")


def process_one(lab, world_provider):
    now = int(time())
    lease = secrets.token_hex(16)
    with lab.connect() as db:
        row = db.execute("SELECT id,template_id,revision FROM template_scene_jobs WHERE status='queued' "
                         "OR (status='running' AND leased_until<=?) ORDER BY rowid LIMIT 1", (now,)).fetchone()
        if row is None:
            return False
        identifier, template_id, revision = row
        db.execute("UPDATE template_scene_jobs SET status='running',lease=?,leased_until=?,"
                   "stage='preparing',started_at=?,updated_at=? WHERE id=?",
                   (lease, now + LEASE_SECONDS, now, now, identifier))
    job = identifier, lease
    try:
        model = Model()
    except ValueError:
        error = "model_unconfigured"
    else:
        if hasattr(model, "progress"):
            model.progress = lambda stage: _progress(lab, job, stage)
        finished = Event()
        heartbeat = Thread(target=_keep_lease, args=(lab, job, finished), daemon=True)
        heartbeat.start()
        try:
            error = _generate(lab, world_provider, model, job, template_id, revision)
        finally:
            finished.set()
            heartbeat.join()
    if error:
        logging.getLogger(__name__).warning("Template scene job %s failed (%s)", identifier, error)
        with lab.connect() as db:
            db.execute("UPDATE template_scene_jobs SET status='failed',error=?,lease=NULL,leased_until=NULL,"
                       "updated_at=? WHERE id=? AND lease=?", (error, int(time()), *job))
    return True


async def consume(lab_provider, world_provider, stop):
    while not stop.is_set():
        try:
            await asyncio.to_thread(lambda: process_one(lab_provider(), world_provider))
        except Exception as exc:
            logging.getLogger(__name__).warning("Template scene queue unavailable (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
