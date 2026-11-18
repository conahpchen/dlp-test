"""Loopback model and research control listener for isolated deployments."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from business.storage import restore
from dws.world import World
from replay.engine import Engine
from research.api import current_lab, install_routes
from research.worker import consume, enabled


def checkpoint():
    engine = Engine(World({"meta": {"seed": "checkpoint-loader"}}))
    state = restore(engine)
    if state is None:
        raise RuntimeError("Business DWS checkpoint is unavailable")
    return engine, state


@asynccontextmanager
async def lifespan(_app):
    if not enabled():
        yield
        return
    stop = asyncio.Event()
    task = asyncio.create_task(consume(current_lab, stop))
    try:
        yield
    finally:
        stop.set()
        await task


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
install_routes(app, lambda: checkpoint()[0], lambda: checkpoint()[1]["events"], surface="control")
