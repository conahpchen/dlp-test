"""共享 fixtures：演示世界种子与事件（tests/fixtures 由 gen_demo_fixtures 生成）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO


@pytest.fixture(scope="session")
def demo_seed() -> dict:
    return json.loads((FIXTURES / "world_seed.demo.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def demo_events() -> list:
    from replay.harness import load_events
    return load_events(FIXTURES / "events.demo.jsonl")
