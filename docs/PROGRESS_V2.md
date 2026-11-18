# v2.1 实施与测试记录

2026-11-18。基线 `83fad94173af49a9106ca58e7e0a9daa6d390506` 经 `git fetch origin main` 确认。实现提交 `1777bf2`，部署加固 `cdd49bd`。[Draft PR #2](https://github.com/conahpchen/dlp-test/pull/2) 关联 Issue #1。

**状态：阶段性成果供审阅，整体交付条件尚未满足。**

| 验证 | 实际结果 |
|---|---|
| 基线 `.venv/bin/python -m pytest -q` | 360 通过，1 条 Starlette TestClient 弃用警告。默认 Python 缺少 pytest，使用仓库虚拟环境。 |
| 基线 `python -m replay.harness verify --world tests/fixtures/world_seed.demo.json --events tests/fixtures/events.demo.jsonl` | 两次逐字节一致，响应校验和前缀 `0959460e3d996b71`，差异前缀 `b7e8175b0d9002bb`。 |
| 基线 `python -m replay.acceptance` | 10,000 轨迹，164,003 事件，0 不变量违反，0 确定性失败。 |
| 失败复现 `.venv/bin/python -m pytest -q tests/test_v2_versioning.py` | 实现前 3 项按预期失败，分别是模型清单跟随新版本、重启丢失会话、Web 缺少绑定。 |
| 变更后 `.venv/bin/python acceptance.py --full` | 旧版自动验收 7/7。pytest 370 通过、1 条同类警告。两次回放逐字节一致，响应前缀 `058ccbc482449ec4`，差异前缀 `5b9404d896bd7bae`。10,000 轨迹、164,003 事件，0 违反、0 确定性失败。 |
| MySQL `pytest tests/test_mysql_catalog.py` | PyMySQL 真 TCP 握手，拒绝错密码，查询真实 DWS checkpoint 投影，新旧连接读取各自的版本，拒绝 DDL。 |
| Cowrie 本地容器 | 首次镜像拉取 HTTP 503，重试成功。Cowrie 3.1.7 镜像摘要 `sha256:e7c4b4fe5fbb874e682c6c0c7a1daf1c3183c622d5cc2d281f453c1c4d9f97ac`。通过 `docker run -p 127.0.0.1::2222`，用合成 userdb 与 `ssh -F /dev/null` 登录后执行 `ls /etc` 和 `cat /etc/hostname`，两者一致反映容器内 Cowrie 模拟文件系统。该文件系统仍独立于 DWS。 |
| Web、假 AI 和管理面 `pytest tests/test_v2_loopback.py` | 三个真实 loopback HTTP 监听器。控制和 Dashboard 路由拒绝未授权，访客进程不注册研究控制路由；假 AI 完成对话，无内部审计头。用部署脚本启动实况后捕获了 [Dashboard 截图](screenshots/dashboard-v2-runtime.png)。 |
| Canary `pytest tests/test_research.py tests/test_v2_versioning.py` | HTTP ASGI 发送后记录收据，A 重读稳定，B 令牌不同。发送过的 HTTP 标记作为假 AI Bearer 提交时被记录为关联证据，鉴权仍返回 401。SSH 与 MySQL 凭据未打通。 |
| HTTP 发布及故障注入 | 注入缺失入口节点，校验拒绝，重启后先前发布可用并可回滚。通过本机 control 监听器调用已配置的真实模型，成功生成、校验和启用场景，访客可访问。回滚后已绑定访客继续重定向至原场景（307），新访客返回原业务入口（200）。最初禁用环境代理导致连接失败（HTTP 502），恢复既有代理支持后联调通过。 |
| 配置校验 | `sh -n deploy/run-v2-local.sh` 和 `POSTGRES_PASSWORD=local-test-only docker compose -f deploy/docker-compose.yml config -q` 通过。完整 Compose 栈未运行。 |

环境为 Python 3.12.14、pytest 9.2.0、FastAPI 0.163.1、Uvicorn 0.54.0、mysql-mimic 3.0.5、PyMySQL 1.2.4 与 Docker Engine 29.9.2。

剩余验收包括 Cowrie DWS 终端投影、统一跨协议版本绑定、DWS 发布状态机与通用服务规划、SSH/MySQL Canary 与关联撤销、Agent 登记、生产出站网络和权限隔离、完整 Dashboard 页面、在线费用与熔断、真实对照实验。逐项依赖见 [需求追踪](REQUIREMENTS_TRACEABILITY.md)。
