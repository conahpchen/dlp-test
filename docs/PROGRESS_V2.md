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


## Draft PR 后续更新

- 修复 SSH 模拟写入出现在后续会话的视图中。先复现，再验证写入会话在重启后仍能读到该文件，公共视图和新会话保持隔离。
- 管理端新增场景模板配置，可新增模板或追加修订。回环测试经管理端保存模板，从独立控制面按修订创建场景，并从访客监听器访问该场景。测试也覆盖未授权访问和修订冲突。
- 更新后 `.venv/bin/python -m pytest -q` 为 372 通过，1 条既有弃用警告。确定性回放校验通过。
- 整体验收仍未通过。场景模板用于 Web 研究 run；通用合成数据编辑、DWS 发布审批和跨协议应用仍待实现。


### Canary 四入口验证

- `HONEYNET_TEST_COWRIE_IMAGE=cowrie/cowrie:latest .venv/bin/python -m pytest -q` 377 项通过，含 Cowrie 3.1.7 容器与真实 OpenSSH、PyMySQL 客户端联调。
- 单枚已发送 HTTP Canary 可在 Web 核验，并与假 AI Bearer、MySQL 密码认证证明和 Cowrie 登录日志归集为同一凭据的使用证据。证明记录未合并现实身份，也未为 Web 作用域凭据授予 MySQL 或假 AI 权限。Cowrie 桥接导出文件未包含明文凭据。
- 实测错误 MySQL 密码、到期和撤销拒绝新增验证匹配。证据关联可以独立撤销和恢复，原始观测、时间及修订历史在重启后可查询。
- Cowrie 终端的 DWS 投影、统一跨协议版本绑定、通用发布控制面和生产网络隔离仍有缺项。

### 真实 Cowrie 终端的 DWS 投影

- 已实现从权威 DWS checkpoint 导出独立的合成文件投影。Cowrie 3.1.7 使用自身 SSH 协议栈和沙箱文件系统，在建立终端时加载单个版本；投影目录只读挂载。
- 实际 OpenSSH 连接验证了旧终端保持旧文件内容、新终端读取新文件与 DWS 主机地址，以及会话内 `touch` 结果对新会话不可见。同一 DWS 提交改变假 AI 的模型清单，旧会话与新请求分别看到旧值和新值。
- `HONEYNET_TEST_COWRIE_IMAGE=cowrie/cowrie:latest .venv/bin/python -m pytest -q` 共 378 项通过，1 条既有依赖弃用警告。`docker build -f deploy/Dockerfile.cowrie-dws -t dlp-cowrie-dws:local .` 和 `sh -n deploy/run-v2-local.sh` 均通过。
- 实际覆盖合成文件、目录和指定系统文件。完整权限语义、SSH 握手与其他协议之间的统一绑定仍待完成。各投影的原子共享激活、新方案的生产隔离、完整管理端和其他 P2–P5 条件仍未通过整体验收。

### DWS 与 Cowrie 本地原子发布

- 新增同一文件系统的 SQLite 双库事务。DWS 私有 checkpoint 和 Cowrie 只读合成投影使用回滚日志，在同一个事务提交。JSON 实例保留兼容路径。
- 故障注入覆盖两边已写入但未提交时抛出异常、退出写入进程，以及假 AI 请求写入失败后恢复内存事件状态。读取保持在旧版本，后续重试可提交。
- 真实 Cowrie 固定版本测试、本机多监听器测试、MySQL 只读目录客户端测试均切换到 SQLite 路径。重建 Cowrie 镜像和 `sh -n deploy/run-v2-local.sh` 通过。
- 完整测试命令为 `HONEYNET_TEST_COWRIE_IMAGE=cowrie/cowrie:latest .venv/bin/python -m pytest -q`。结果为 381 项通过、1 条既有弃用警告。新版整体验收仍有缺项。该事务范围为本机 checkpoint 和 Cowrie 投影，通用 Proposal、Web 激活、网络隔离及外部服务编排仍需验收。


### 管理端合成文档审批发布

- 新增本地配置页，支持载入现有合成文档、新建草稿、审批和撤回。一个 DWS 单写入者处理已批准队列，应用标记和投影数据一起提交。
- `pytest tests/test_document_publications.py` 实测访客入口没有管理接口、未授权无法创建、草稿不投放、故障回滚与自动重试、陈旧草稿拒绝、旧 Web 场景维持原版本和撤回后旧版本可读。
- 当前覆盖合成知识库文档。通用对象或服务提案、各协议会话绑定、生产网络隔离和完整 P2–P5 整体验收仍有缺项。
- 包含真实 Cowrie 的全量套件首轮遇到 Docker 启动超时，复跑 `HONEYNET_TEST_COWRIE_IMAGE=cowrie/cowrie:latest .venv/bin/python -m pytest -q` 为 382 项通过、1 条既有弃用警告。确定性回放字节比对和部署脚本语法检查通过。


### 模型运行预算和后台适配

- SQLite 持久化每日 token 与价格额度预留。精确缓存隔离输入，返回结果使用拷贝并设 TTL；熔断和半开探测在控制进程执行。
- 分离部署新增有界后台队列。同一 run 的同一会话请求合并；租约和修订号避免将处理中新增任务误删。控制面才调用模型。
- `pytest tests/test_model_budget.py` 使用受控传输实测预算跨实例生效、超额不外呼、缓存不重复外呼、连续供应商故障触发熔断、冷却后恢复。HTTP 测试验证现有场景在预算拒绝时继续响应，已分类任务排队后可以返回处理结果。
- 成本为预留估计。真实供应商故障演练、网络隔离、通用跨服务发布和其余新版整体验收仍有缺项。
- 最新全量命令 `HONEYNET_TEST_COWRIE_IMAGE=cowrie/cowrie:latest .venv/bin/python -m pytest -q` 为 385 项通过，含真实 Cowrie/OpenSSH 和 PyMySQL 路径；1 条既有依赖弃用警告。
