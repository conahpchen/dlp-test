# v2.1 验收

| 验收项 | 执行方式 | 本轮结果 |
|---|---|---|
| 确定性 | `python -m replay.harness verify --world tests/fixtures/world_seed.demo.json --events tests/fixtures/events.demo.jsonl` 与 `python -m replay.acceptance` | 通过，见进度报告 |
| 会话冻结及重启 | `pytest tests/test_v2_versioning.py` | SSH 回放、假 AI 功能路由、Web 绑定及 checkpoint 单写恢复 |
| 真实协议 | `pytest tests/test_mysql_catalog.py tests/test_v2_loopback.py` | MySQL 握手、认证、只读查询及 HTTP/假 AI 独立监听器；Cowrie 实际终端仍待测 |
| 诱饵 | `pytest tests/test_v2_versioning.py -k lure` | Web A/B 隔离、A 重读一致与核验，HTTP ASGI 响应发送记录，已发送标记在假 AI 入口命中；SSH 和 MySQL 核验待实现 |
| 场景故障 | `pytest tests/test_v2_versioning.py -k activation` | Mock 模型生成，故障场景校验拒绝，经重启恢复并回滚 HTTP 活跃场景 |
| 模型准入 | `pytest tests/test_input_generation.py` | 含未知路径、字段、文本的请求不会进入 Mock 模型 prompt |
| 管理面 | `pytest tests/test_dashboard_runtime.py tests/test_v2_loopback.py` | 授权及独立监听器、运行数据和实际响应 p95；真实模型与网络策略未实测 |

整体交付条件尚未满足。待完成项见需求追踪表。
