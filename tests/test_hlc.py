"""HLC：解析 / 全序 / 时钟单调（契约 12 §1.3）。"""
from __future__ import annotations

from dws.hlc import Clock, HLC


def test_parse_roundtrip():
    h = HLC.parse("1730000000.0001/sensor-a")
    assert h.seconds == 1730000000 and h.counter == 1 and h.node == "sensor-a"
    assert str(h) == "1730000000.0001/sensor-a"


def test_ordering_not_string_compare():
    # 字符串比较会得出错误结论（"999..." > "1730..."），key 比较必须正确
    assert HLC.parse("999.0001/a") < HLC.parse("1730000000.0000/b")
    # 全序 = (物理秒, 计数器, node)：计数器先于 node
    assert HLC.parse("1730000000.0001/a") < HLC.parse("1730000000.0002/a")
    assert HLC.parse("1730000000.0001/b") < HLC.parse("1730000000.0002/a")
    assert HLC.parse("1730000000.0001/a") < HLC.parse("1730000000.0001/b")


def test_clock_strictly_monotonic():
    c = Clock("replay", 1_730_000_000)
    seen = []
    for i in range(50):
        ext = HLC.parse(f"1730000000.{i:04d}/sensor-a")
        seen.append(c.after(ext))
    keys = [h.key() for h in seen]
    assert keys == sorted(keys) and len(set(keys)) == 50


def test_clock_absorbs_external_time():
    c = Clock("replay", 1_730_000_000)
    future = HLC.parse("9999999999.0007/sensor-b")
    nxt = c.after(future)
    assert nxt.seconds == 9999999999 and nxt.node == "replay"
    assert nxt > future
