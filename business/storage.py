"""Atomic local checkpoint preserving every DWS fact version; single worker only."""

import json
import os
from pathlib import Path
from dws.facts import Fact
from dws.hlc import HLC


def location():
    value = os.environ.get("BUSINESS_STATE_FILE")
    if value == "":
        return None
    if value:
        return Path(value)
    if "GATEWAY_WORLD_SEED" in os.environ or "HONEYNET_PG_DSN" in os.environ:
        return None
    return Path(__file__).resolve().parents[1] / "var" / "business-state.json"


def restore(engine):
    path = location()
    if not path or not path.exists():
        return None
    state = json.loads(path.read_text())
    if state["schema"] != 1:
        raise ValueError("Unsupported business checkpoint schema")
    engine.world.meta = state["meta"]
    engine.world.facts = {
        cls: {eid: [Fact(**f) for f in rows] for eid, rows in entities.items()}
        for cls, entities in state["facts"].items()
    }
    engine.biz_seq = state["biz_seq"]
    clock = HLC.parse(engine.world.meta["now_hlc"])
    engine.clock.seconds = clock.seconds
    engine.clock.counter = clock.counter
    return state


def persist(engine, audit, events):
    path = location()
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    state = dict(
        schema=1,
        meta=engine.world.meta,
        biz_seq=engine.biz_seq,
        facts={
            cls: {eid: [f.to_row() for f in rows] for eid, rows in entities.items()}
            for cls, entities in engine.world.facts.items()
        },
        audit=audit,
        events=events,
    )
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as out:
        json.dump(state, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
