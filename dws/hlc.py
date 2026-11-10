"""混合逻辑时钟（契约 12 §1.3）。

格式: "<物理秒>.<计数器:04d>/<node_id>"，如 1730000000.0001/sensor-a。
全序 = (物理秒, 计数器, node 字典序)。时间只用 HLC，不用墙钟（K5）。

注意: HLC 字符串**不可直接字典序比较**（秒位数可变），一律 parse 后按 key 比较。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HLC:
    seconds: int
    counter: int
    node: str

    @staticmethod
    def parse(s: str) -> "HLC":
        ts, node = s.split("/", 1)
        sec, cnt = ts.split(".", 1)
        return HLC(int(sec), int(cnt), node)

    def key(self):
        return (self.seconds, self.counter, self.node)

    def __lt__(self, other):
        return self.key() < other.key()

    def __le__(self, other):
        return self.key() <= other.key()

    def __gt__(self, other):
        return self.key() > other.key()

    def __ge__(self, other):
        return self.key() >= other.key()

    def __str__(self):
        return f"{self.seconds}.{self.counter:04d}/{self.node}"


class Clock:
    """引擎侧逻辑时钟：每次 after() 严格 +1，同 node 全序由构造保证（I1 时间线单调的来源）。

    确定性：base_seconds 来自世界种子（dws_meta.seed 推导），吸收的事件 ts_hlc 也来自输入，
    因此回放两次的时钟轨迹完全一致。
    """

    def __init__(self, node: str, base_seconds: int):
        self.node = node
        self.seconds = base_seconds
        self.counter = 0

    def peek(self) -> HLC:
        return HLC(self.seconds, self.counter, self.node)

    def after(self, other: "HLC | None") -> HLC:
        """吸收外部时间并推进，返回新 HLC（严格大于本 node 此前任意值）。"""
        if other is not None:
            if other.seconds > self.seconds:
                self.seconds = other.seconds
                self.counter = other.counter + 1
            elif other.seconds == self.seconds:
                self.counter = max(self.counter, other.counter) + 1
            else:
                self.counter += 1
        else:
            self.counter += 1
        return self.peek()
