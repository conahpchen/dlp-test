# v2.1 验收

| 验收项 | 执行方式 | 本轮结果 |
|---|---|---|
| 确定性 | `python -m replay.harness verify --world tests/fixtures/world_seed.demo.json --events tests/fixtures/events.demo.jsonl` 与 `python -m replay.acceptance` | 通过，见进度报告 |
| 会话冻结及重启 | `pytest tests/test_v2_versioning.py` | SSH 回放、假 AI 功能路由、Web 绑定及 checkpoint 单写恢复 |
| 真实协议 | `pytest tests/test_mysql_catalog.py tests/test_v2_loopback.py` | MySQL 握手、认证、只读查询及 HTTP/假 AI 独立监听器；另用真实 Cowrie 测试旧新会话的 DWS 文件投影 |
| 诱饵 | `pytest tests/test_v2_versioning.py -k lure` | Web A/B 隔离、A 重读一致与核验，HTTP ASGI 响应发送记录，已发送标记在假 AI 和真 MySQL 协议入口命中。密码错误、过期与撤销均拒绝新增匹配。启用 Cowrie 容器验收可验证同一枚 Canary 在四个入口的证据归集 |
| 场景故障 | `pytest tests/test_v2_versioning.py -k activation` | Mock 模型生成，故障场景校验拒绝，经重启恢复并回滚 HTTP 活跃场景 |
| 模型准入 | `pytest tests/test_input_generation.py` | 含未知路径、字段、文本的请求不会进入 Mock 模型 prompt |
| 管理面 | `pytest tests/test_dashboard_runtime.py tests/test_v2_loopback.py` | 授权及独立监听器、运行数据和实际响应 p95；真实模型与网络策略未实测 |

整体交付条件尚未满足。待完成项见需求追踪表。

启用真实 Cowrie Canary 集成测试时执行 `HONEYNET_TEST_COWRIE_IMAGE=cowrie/cowrie:latest python -m pytest -q tests/test_cowrie_canary_integration.py`。该测试需要本机 Docker、Cowrie 镜像、OpenSSH 客户端。

真实 Cowrie 投影使用 `HONEYNET_TEST_COWRIE_IMAGE=cowrie/cowrie:latest python -m pytest -q tests/test_cowrie_projection_integration.py`。同一 DWS 提交分别改变文件和假 AI 模型列表，已有会话保留各自的初始版本。


本机联合演练统一执行 `deploy/check-v2.sh`，前置镜像和演示入口见 [DEMO_V2.md](DEMO_V2.md)。覆盖真实 Cowrie/OpenSSH、L2 和 L3 MySQL、L3 Nginx Unix socket、OA 与假 AI HTTPS 入口、控制面和 Dashboard。目标生产机器的外部网关和权限验收单独记录。
