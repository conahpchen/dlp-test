"""统一事件 schema v1 契约测试（12 §1.4）。"""
from __future__ import annotations

import copy

import pytest

from replay.harness import validate_events


def test_demo_events_valid(demo_events):
    validate_events(demo_events)


@pytest.mark.parametrize("mutation", [
    lambda e: e.pop("session"),                                   # 缺必填
    lambda e: e.update(entry="smtp"),                             # 非法入口
    lambda e: e.update(event_id="not-an-ulid"),                   # ULID 模式
    lambda e: e.update(ts_hlc="2026-10-23T09:00:00Z"),            # 墙钟（禁止）
    lambda e: e.update(verdict=5),                                # verdict 非对象/null
    lambda e: e.update(extra_field=1),                            # additionalProperties
])
def test_invalid_events_rejected(demo_events, mutation):
    broken = copy.deepcopy(demo_events[:1])
    mutation(broken[0])
    with pytest.raises(Exception):
        validate_events(broken)
