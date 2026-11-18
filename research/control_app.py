"""Loopback model and research control listener for isolated deployments."""

from fastapi import FastAPI

from business.storage import restore
from dws.world import World
from replay.engine import Engine
from research.api import install_routes


def checkpoint():
    engine = Engine(World({"meta": {"seed": "checkpoint-loader"}}))
    state = restore(engine)
    if state is None:
        raise RuntimeError("Business DWS checkpoint is unavailable")
    return engine, state


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
install_routes(app, lambda: checkpoint()[0], lambda: checkpoint()[1]["events"], surface="control")
