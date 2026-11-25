"""Share template generation requests between the Dashboard and the model control process."""

import asyncio
import logging
import secrets
from time import time

from research.generation import VerificationRequired
from research.model import Model
from research.model_budget import InferenceUnavailable


MAX_PENDING = 32
JOB_STATE = "SELECT id,status,error FROM template_scene_jobs WHERE template_id=? AND revision=?"


def _state(row):
    if row is None:
        return None
    identifier, status, error = row
    return {"status": status, "run_id": identifier if status == "ready" else None, "error": error}


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
        db.execute("INSERT OR REPLACE INTO template_scene_jobs VALUES (?,?,?,'queued',NULL,NULL,NULL)",
                   (template_id, revision, identifier))
        return _state((identifier, "queued", None))


def _generate(lab, world_provider, scene_job, template_id, revision):
    try:
        model = Model()
    except ValueError:
        return "model_unconfigured"
    try:
        world = world_provider()
    except RuntimeError:
        return "world_unavailable"
    try:
        template = lab.template(template_id, revision)
        variant = template["variant"]
        lab.create(world, variant, model, template["goal"], template=template, scene_job=scene_job,
                   require_verification=variant == "agent_note")
    except InferenceUnavailable:
        return "model_unavailable"
    except VerificationRequired:
        return "verification_missing"
    except Exception as exc:
        logging.getLogger(__name__).warning("Template scene generation failed (%s)", type(exc).__name__)
        return "generation_failed"
    return None


def process_one(lab, world_provider):
    now = int(time())
    lease = secrets.token_hex(16)
    with lab.connect() as db:
        row = db.execute("SELECT id,template_id,revision FROM template_scene_jobs WHERE status='queued' "
                         "OR (status='running' AND leased_until<=?) ORDER BY rowid LIMIT 1", (now,)).fetchone()
        if row is None:
            return False
        identifier, template_id, revision = row
        # Allow for two drafts and two reviews at the maximum model timeout.
        db.execute("UPDATE template_scene_jobs SET status='running',lease=?,leased_until=? WHERE id=?",
                   (lease, now + 900, identifier))
    error = _generate(lab, world_provider, (identifier, lease), template_id, revision)
    if error:
        with lab.connect() as db:
            db.execute("UPDATE template_scene_jobs SET status='failed',error=?,lease=NULL,leased_until=NULL "
                       "WHERE id=? AND lease=?", (error, identifier, lease))
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
