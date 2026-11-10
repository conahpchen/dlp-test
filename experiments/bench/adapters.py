"""各蜜网系统的 adapter：drive（驱动活蜜罐，可选）+ parse_logs（日志 → 统一事件）。

已实现：cowrie（复用 entries/cowrie/bridge 的映射与合规变换）。
骨架：tpot / galah / opencanary——日志格式在其容器部署后按实测样本补齐
（不留臆测实现；实测样本进来当天即可填好）。
"""

from __future__ import annotations

import json
from pathlib import Path

from entries.cowrie.bridge import convert as cowrie_convert


class Adapter:
    name = "base"

    def drive(self, actions: list[dict], target: dict) -> None:
        """把脚本动作打到活蜜罐（target: {host, ports}）。默认不支持。"""
        raise NotImplementedError(f"{self.name} adapter 暂不支持活驱动")

    def parse_logs(self, log_path: Path, dst_host: str) -> list[dict]:
        raise NotImplementedError

    def validate(self, events: list[dict]) -> list[dict]:
        from replay.harness import validate_events
        validate_events(events)
        return events


class CowrieAdapter(Adapter):
    """Cowrie（BSD-3）：json 日志 → 统一事件。复用 M0 桥接的映射与合规变换。"""

    name = "cowrie"

    def parse_logs(self, log_path: Path, dst_host: str = "web01") -> list[dict]:
        events, counter, skipped = [], 0, 0
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                ev = cowrie_convert(record, counter, "sensor-a", dst_host)
                if ev is None:
                    skipped += 1
                    continue
                counter += 1
                events.append(ev)
        if skipped:
            print(f"[{self.name}] 跳过 {skipped} 行非映射记录")
        return events


class TPotAdapter(Adapter):
    """T-Pot（Apache-2.0）：多蜜罐套件。日志按其内各蜜罐（cowrie/dionaea/...）
    分别落在 /data/…；部署后以实测样本补齐各子解析器（映射到统一 schema）。"""

    name = "tpot"


class GalahAdapter(Adapter):
    """Galah（Apache-2.0）：LLM Web 蜜罐，LLM 后端指向我们的网关 :4000。
    日志为 JSON（gpt 交互记录）；部署后按实测样本补齐。"""

    name = "galah"


class OpenCanaryAdapter(Adapter):
    """OpenCanary（BSD）：低交互多协议，syslog JSON 输出；部署后补齐。"""

    name = "opencanary"


ADAPTERS = {a.name: a for a in
            (CowrieAdapter(), TPotAdapter(), GalahAdapter(), OpenCanaryAdapter())}
