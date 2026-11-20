"""Loopback model and research control listener for isolated deployments."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from business.storage import restore
from dws.world import World
from replay.engine import Engine
from research.api import current_lab, install_routes
from research.worker import automatic_enabled, consume, enabled


def checkpoint():
    engine = Engine(World({"meta": {"seed": "checkpoint-loader"}}))
    state = restore(engine)
    if state is None:
        raise RuntimeError("Business DWS checkpoint is unavailable")
    return engine, state


async def monitor_releases(stop, interval=5):
    prior_failure = None
    while not stop.is_set():
        try:
            lab = current_lab()
            result = await asyncio.to_thread(lab.compensate_unready, inspect_only=True)
            failure = (result["active_run"], tuple(result["failed"])) if result["status"] == "unready" else None
            if failure is not None and failure == prior_failure:
                await asyncio.to_thread(lab.compensate_unready)
                failure = None
            prior_failure = failure
        except Exception as exc:
            prior_failure = None
            logging.getLogger(__name__).warning("Release health probe failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def bootstrap_scene(stop, interval=30):
    """Prepare the shared HTTP scene used by the existing session worker."""
    from research.model import Model

    run = None
    while not stop.is_set():
        try:
            lab = current_lab()
            if await asyncio.to_thread(lab.active):
                return
            if run is None:
                engine, _ = await asyncio.to_thread(checkpoint)
                result = await asyncio.to_thread(lab.create, engine.world, "plain", Model(),
                                                 automatic=True)
                run = result["run_id"]
            await asyncio.to_thread(lab.activate, run, only_if_inactive=True)
            return
        except Exception as exc:
            logging.getLogger(__name__).warning("Automatic scene initialization pending (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(_app):
    stop = asyncio.Event()
    tasks = []
    if automatic_enabled():
        tasks.append(asyncio.create_task(bootstrap_scene(stop)))
    if enabled():
        tasks.append(asyncio.create_task(consume(current_lab, stop)))
    if os.environ.get("HONEYNET_RELEASE_MONITOR") == "1":
        tasks.append(asyncio.create_task(monitor_releases(stop)))
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(*tasks)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
install_routes(app, lambda: checkpoint()[0], lambda: checkpoint()[1]["events"], surface="control")
