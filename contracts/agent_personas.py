"""人格配置加载（contracts/agent_personas.yaml）。"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PERSONAS_PATH = REPO_ROOT / "contracts" / "agent_personas.yaml"


def load_personas() -> list[dict]:
    data = yaml.safe_load(PERSONAS_PATH.read_text(encoding="utf-8"))
    return data["personas"]
