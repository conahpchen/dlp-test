"""泄漏守卫（契约 12 §8 第 5 步）。

M0 硬检查（宁窄勿全，后续里程碑追加规则）：
1. 响应中出现的 IPv4 必须落在已声明网段或 loopback——防止把"世界外"的地址说出去；
2. 出现真实资产占位标记即拦截（部署时通过环境/配置注入真实标记清单）。

命中即判 GroundingMiss → 引擎 fail-closed 走兜底模板（ADR-04：矛盾优先于深度）。
"""
from __future__ import annotations

import ipaddress
import re

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# 部署侧可注入的真实资产标记（12 §8 泄漏守卫扩展点；M0 用内置占位）
_DEFAULT_FORBIDDEN = ("REAL_PII", "PROD_SECRET", "real-corp.example")


def leak_guard(lines, world) -> list[str]:
    problems: list[str] = []
    subnets = []
    for s in world.meta.get("subnets", []):
        try:
            subnets.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            problems.append(f"dws_meta.subnets 含非法网段: {s}")

    forbidden = tuple(_DEFAULT_FORBIDDEN) + tuple(world.meta.get("forbidden_tokens", ()))

    for line in lines:
        for raw in _IPV4.findall(line):
            try:
                ip = ipaddress.ip_address(raw.split("/")[0])
            except ValueError:
                continue
            if not (ip.is_loopback or any(ip in n for n in subnets)):
                problems.append(f"未声明网段的 IP 出现在响应: {raw}")
        for tok in forbidden:
            if tok in line:
                problems.append(f"疑似真实资产标记: {tok}")
    return problems
