"""G1 pivot 演示端到端（11 §04 M1 验收的三项结论，见 experiments/pivot_demo.py）。"""

from __future__ import annotations

from experiments.pivot_demo import main


def test_pivot_demo_passes():
    assert main() == 0
