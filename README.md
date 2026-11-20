# 澄川研发服务平台 · AI 蜜网

面向授权环境的防御型蜜网。访客可连接预置 OA、Cowrie SSH、轻量 MySQL 和模拟 AI API。各入口使用合成业务资料，Dashboard 提供运行指标、告警和会话证据。启用自动模式后，控制面可根据会话行为生成和校验蜜饵，并投放到已有模拟入口。

- [项目介绍](docs/PROJECT_OVERVIEW_ZH.md) — 能力、模块和工作原理
- [部署实施手册](docs/DEPLOYMENT_RUNBOOK_ZH.md) — 安装、启动、验收和回退
- [演示与自动模式](docs/DEMO_V2.md) — 本地演示和自动投放配置
- [设计与实现索引](docs/README.md) — 详细架构、接口和研究说明
- [需求追踪](docs/REQUIREMENTS_TRACEABILITY.md) · [历史实测记录](docs/PROGRESS_V2.md)

## 本地验证

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m replay.harness verify --world tests/fixtures/world_seed.demo.json \
    --events tests/fixtures/events.demo.jsonl
.venv/bin/python -m replay.acceptance
```

含真实容器的联调使用 `deploy/check-v2.sh`，镜像参数见[演示与自动模式](docs/DEMO_V2.md)。
