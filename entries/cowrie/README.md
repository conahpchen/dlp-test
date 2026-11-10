# entries/cowrie —— SSH 入口（M0）

Cowrie（BSD-3）作为 SSH/Telnet 高交互入口；本目录只包含两样东西：

1. `cowrie.cfg.example` —— 让 Cowrie 输出 JSON 日志（桥接的数据源）；
2. `bridge.py` —— Cowrie JSON 日志 → 统一事件 v1（契约 12 §1）。

## 本地（无 Docker）联调

```bash
# 1. 用 tests/fixtures/cowrie.sample.jsonl 模拟 Cowrie 日志
python -m entries.cowrie.bridge --in ../tests/fixtures/cowrie.sample.jsonl \
       --out events.jsonl --node sensor-a --dst-host web01
# 2. 用演示世界回放这批事件
python -m replay.harness run --world ../tests/fixtures/world_seed.demo.json \
       --events events.jsonl --out ../../artifacts/bridge-demo
```

## 内网 Linux 部署（阶段一）

`deploy/docker-compose.yml` 已编排 cowrie + postgres + redis + bridge；
桥接以 `--follow` 模式持续转换 Cowrie 日志并发布到 Redis `events.raw.ssh`。
端口全部绑定 `HONEYNET_BIND`（默认 127.0.0.1），公网迁移见 13 §3.3 checklist。

## 合规红线（T6）

- 密码字段在桥接内立即哈希，明文不落任何输出；
- src.ip /24 截断后存储；
- 原始日志（cowrie.json）只留在 sensor 本地，分析链路只吃结构化字段（05 §01 原则 5）。
