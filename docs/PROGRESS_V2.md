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
| Web、假 AI 和管理面 `pytest tests/test_v2_loopback.py` | 三个真实 loopback HTTP 监听器。控制和 Dashboard 路由拒绝未授权，访客进程不注册研究控制路由；假 AI 完成对话，无内部审计头。用部署脚本启动实况后捕获了 [Dashboard 截图](https://github.com/conahpchen/dlp-test/blob/24d23479fc160a34e39558deb600e920d09137bc/docs/screenshots/dashboard-v2-runtime.png)。 |
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


### Agent 可选登记与会话证据

- 已接入真实诱饵响应的登记链接。按 run 和来源会话核验投放收据，仅接受四项闭集枚举，超长正文、额外字段、自由文本会被拒绝且不回显。
- 管理端可按实际 run 浏览 Web 时间线、Canary 命中与修订。登记把自报、服务端观测和行为推断分栏。Dashboard 显示 UTC 当日实际预留 token 和按配置估算的费用。
- `pytest tests/test_agent_registration.py` 覆盖访问隔离、重复登记、相同客户端特征的多个会话仍然独立、敏感字段拒绝、真实 ledger 汇总和证据来源字段。
- 含真实 Cowrie/OpenSSH 和 PyMySQL 的全量测试为 386 项通过、1 条既有依赖弃用警告。


### 服务能力规划与激活审核

- 场景发布编译知识库和现有 HTTP 入口，生成版本化发布清单。启用后的基础场景根据清单允许 HTTP 路由。模型声明只能引用静态能力，不携带端口、地址或命令。
- MySQL 目录能力需要本机真实握手、认证和只读查询成功。L3 能力在缺少批准的隔离运行时的当前部署中被拒绝。
- `pytest tests/test_service_planner.py` 验证实际 MySQL 客户端失败与成功、失败时保留活跃场景、旧会话绑定、回滚、管理端鉴权和清单控制真实 HTTP 路由。
- MySQL 准备检查覆盖预配置的本机运行时。跨服务通用发布、L3 服务生命周期和生产网络隔离仍需整体验收。
- 启用真实 Cowrie/OpenSSH 与 PyMySQL 的全量测试 388 项通过，1 条既有依赖弃用警告。确定性回放和发布页面脚本语法检查通过。


### 三协议版本绑定、依赖补偿和 Linux 入口隔离

- 操作员签发受时效控制的显式版本选择票据。测试使用同一票据在 Web、集成假 AI API 和真实 MySQL 选择旧的 DWS 版本，新请求读取新版本。假 AI 和 MySQL 仍要求各自的合成凭据。SSH 暂未接入这套票据。
- 真实 MySQL 服务停止后，控制面复查将新访客切回不依赖该服务的先前场景，已绑定访客保留原版本。当前补偿覆盖场景中的预配置 MySQL 依赖。
- Linux 入站模式为 HTTP 和 MySQL 启动器提供系统调用限制。实测真实 HTTP 仍响应，进程发起到活跃本机 TCP/UNIX 端点的连接以及 UDP/外发数据报均得到 EPERM。Cowrie、文件权限及完整生产拓扑未验收。
- `HONEYNET_TEST_COWRIE_IMAGE=cowrie/cowrie:latest .venv/bin/python -m pytest -q` 392 项通过。Linux 受限进程的真实 PyMySQL 入站握手和查询验证通过。确定性回放以及启动脚本语法检查通过。


### 四协议显式绑定、自动补偿与隔离参考拓扑

- Cowrie 在 SSH 认证完成后读取单独传入的版本票据，从只读导出目录载入对应的历史快照。票据不会作为通用环境变量导入或记录，也不会替代 SSH 凭据。
- 控制面按发布清单检查知识库、HTTP 和 MySQL 两类网络运行时，连续两个相同失败时触发补偿。实际关闭本地 HTTP 健康端点，验证后台任务自动停止向新访客投放，同时保留已有会话。真实 MySQL 停机回退到先前场景的用例仍通过。
- 隔离镜像内使用 libseccomp，关闭 SSH 转发与 SFTP，保持 Cowrie 真实入站可用。参考拓扑包括受限的 Cowrie、Web/假 AI、MySQL，以及独立鉴权的控制和 Dashboard 监听器。宿主生产账号权限、外部网关和防火墙尚需目标环境验证。
- `HONEYNET_TEST_COWRIE_IMAGE=cowrie/cowrie:latest HONEYNET_TEST_COWRIE_ISOLATED_IMAGE=dlp-cowrie-isolated:local .venv/bin/python -m pytest -q` 393 项通过，1 条既有依赖弃用警告。隔离镜像默认入口联调单独复跑通过。联合用例同时启动 Cowrie、Web/假 AI、MySQL、控制和 Dashboard，验证四协议同票据、各入口的访问边界及入站响应。


### L3 独立 MySQL

- 完整 MySQL 8.4 实例需经过模型声明、SQL 能力校验、人工显式批准和固定镜像摘要配置。发布前执行真实 Unix socket 登录和关联查询，主机端口不对外发布。数据来自绑定版本的 DWS 文档事实。
- `tests/test_isolated_mysql_release.py` 实测缺少审批、镜像不存在、成功启动、查询 JOIN、客户端事务回滚、Docker 停机后场景补偿、人工回滚、清理容器和临时凭据。首轮 Docker 启动超时后发现一个测试容器停在 Created，已清理并将创建与启动分开，增加本机 Docker 操作超时。组合验证继续使用真实 Cowrie 镜像。
- 本次 L3 范围为获授权的本机 Unix socket 与完整 MySQL，独立 HTTP/其他协议运行时和目标生产环境外部入口仍不在已验收范围。
- 最终组合验证启用 `HONEYNET_TEST_L3_MYSQL_IMAGE=mysql@sha256:a81d043930e51745522bd5c430961cf8286f0a23f0a9a2a6e0108f7ea71b0902`、`HONEYNET_TEST_COWRIE_IMAGE=cowrie/cowrie:latest`、`HONEYNET_TEST_COWRIE_ISOLATED_IMAGE=dlp-cowrie-isolated:local`。执行 `.venv/bin/python -m pytest -q` 得到 394 项通过、1 条既有依赖弃用警告，Docker 无遗留测试 L3 容器。确定性回放也通过。

### 临时部署验收与演示

- 新增 L3 独立 HTTP。真实 Nginx 使用无网络、无宿主端口的容器和本地 Unix socket 提供绑定版本的只读 DWS 投影。控制面审批、健康检查、故障回退和清理与 MySQL L3 共用发布流程。两者共用一个运行额度。
- 临时 HTTPS 网关已通过真实 Nginx 联调。实际 OA、假 AI 请求可通过 TLS，管理、控制、审计和内部健康路径由网关返回 404。外部入口使用本机回环地址和临时测试证书。本次未连接目标生产主机。
- Dashboard 新增部署运行页，显示真实监听器探测、发布及健康结果、最近请求窗口 p95、DWS 变更摘要和预算账本，同时保留模板、数据审批和会话证据页面。Chromium 截图来自真实临时 HTTP 与 Dashboard 实例。
- 全量首轮暴露官方 MySQL 镜像初始化阶段与正式进程切换之间的健康检查竞态。就绪检查现要求 mysqld 已成为容器主进程，再进行认证查询。
- 上机前文档和一键本机验收见 [DEMO_V2.md](DEMO_V2.md)。项目保留的缺项见 [REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md)。
- 今日复测真实模型。首个创建请求返回 HTTP 502，独立生成及审核检查随后通过。通过独立 visitor/control 监听器复跑后，SQLite 中存在真实生成的 run，发布历史顺序为 activate、rollback，访客记录包含一次场景访问。未输出供应商凭据或原始数据。
- 最终从当前代码执行 `deploy/check-v2.sh`，使用原版 Cowrie、隔离版 Cowrie、固定摘要 MySQL 8.4 与固定摘要 Nginx 镜像。396 项通过、1 条既有弃用警告，逐字节校验通过，10,000 轨迹共 164,003 事件，0 不变量违反、0 确定性失败。临时参考环境通过本轮自动演练，目标生产验收及需求追踪余项仍未完成。

## 2026-11-21 架构复核和 Dashboard 实测

在 GitHub `main` 的 `491863086afb24a842b4a6106e3e93a94c8966d0` 基线上，核对了 Issue #1 和后续架构要求。本地新增十个菜单的统一管理界面，可浏览 DWS 版本差异及真实 Canary 链路。隔离环境中使用四个测试镜像运行 `deploy/check-v2.sh`，加入链路页面后再次执行全套 pytest，405 项通过。逐字节回放及 10,000 条轨迹通过。真实 DeepSeek 的 Web 场景和 SSH、MySQL、模拟 AI 各投放路径已执行。生产环境和通用 DWS 图发布等待验收，当前工作树尚未合并。详细覆盖范围和缺口见 [架构符合性审查](ARCHITECTURE_REVIEW_ZH.md)。

### 独立员工活动、观测隔离和态势报告

员工按服务独立排班，OA 只可选安排业务员工查看待办。模拟 AI 的客户端观察、动态蜜饵和 Canary 目标会话按服务签发的会话 cookie 区分，明确的 DWS 版本绑定仍按原有协议执行。控制面可按小时或由实际多入口 Canary 命中触发报告。虚拟环境最终 pytest 406 项通过。回放 10,000 条轨迹和 164,003 个事件均无不变量违规或确定性失败。真实 DeepSeek 非 Web 复验 3 项通过，态势报告页面和证据跳转经过 Chromium 实测。

### SSH 流日志证据与双角色处置

Cowrie 连接证据使用脱敏五元组与导入的默认 VPC Flow Logs 进行精确匹配，只在唯一候选时显示。链路及 Canary 页面连接到「证据处置」，运营提交、独立审核和证据写入经过虚拟环境测试。最终 `deploy/check-v2.sh` 为 408 项通过，逐字节回放及 10,000 条轨迹通过，真实 DeepSeek 复验通过。未接入目标 VPC 或完成整体生产验收。

### DWS 图、四入口证据与自动限额

发布队列现在支持将合成知识文档、数据库目录及其关系写入同一 DWS 事务。Web、Cowrie、MySQL 和模拟 AI 已读取同一会话分支。测试覆盖旧会话隔离、按目标协议分别签发、错误入口拒绝、投影写入失败后的完整回滚、提交成功但队列未确认时的重试、撤回后拒绝新签发。

四个真实协议连接的观测在回环环境中与合成 AWS VPC Flow Logs 配对，Chromium 验证了 Dashboard 上的来源和三个命中入口。HTTP 预算超限返回 429 并关闭 HTTP/1.x 连接。MySQL 连接超限、查询超限和 Cowrie 命令超限均有真实客户端复验。独立实测也确认 MySQL 在下一个窗口可重新接受连接。Dashboard 的图草稿、审批、撤回、移动布局和入口隔离完成 Chromium 演练。

`deploy/check-v2.sh` 运行 410 项测试，全部通过。两次基准回放逐字节一致，10,000 条轨迹及 164,003 个事件均无不变量违规与确定性失败。真实 DeepSeek 的原有 Web 自动生成与审核耗时 8.92 秒，在线更新耗时 8.76 秒。注入超时后保留已发布版本，原场景返回 HTTP 200。

统一图当前覆盖文档和合成目录。模型自动生成路径仍独立运行。各入口自动接续签发下一跳 Canary、通用服务图和运行器发布、跨实例预算和逻辑会话吊销、目标生产环境的权限与恢复验收仍待完成。本轮代码位于本地工作树。

### DWS 实体图驱动发布与逐跳 Canary 复验

后续实现将 Web 初始场景、在线补丁、模拟 HTTP 路由以及 SSH、MySQL 和模拟 AI 生成内容接入同一个 DWS 写入器。模型只接收选定基线的实体小图、有限文档摘录或协议行为枚举。场景须引用一组在 DWS 中真实相连的实体。程序核验关系、节点、能力和数据边界，再将模型复核通过的产物提交。一次受限修复后仍校验失败时，保留原发布。

真实 `deepseek-flash` 的生成与审核用时 13.86 秒，引用 4 个 DWS 实体。确认提交前访客返回 404，提交激活后两条模型新建的模拟 HTTP 路径正确返回内容。独立真实协议演练确认三个新提交的私有 DWS 版本分别在原 Cowrie、MySQL 和模拟 AI 会话中交付。独立会话保持隔离。发布故障注入验证投影与检查点同时回滚，重试后新 HTTP 路由才可用。

允许列表内的独立 Nginx 和 MySQL 均通过实际 Unix socket 读取生成内容和同一发布版本。复验了实例就绪后进程中断导致的激活重试、重复启用不创建第二个实例、运行器停止后的回退和清理。独立运行器使用无网络容器和私有本机 socket。

隔离环境使用 HTTP、OpenSSH、MySQL 协议客户端和模拟 AI 完整跑通 Web→SSH→MySQL→模拟 AI→Web。验证错误入口无法使用、凭据不可换会话重放、后代随根撤销失效。Chromium 已展开场景发布图并验证逐跳父级链接。

最终 `deploy/check-v2.sh` 通过 415 项测试。两次回放逐字节一致，10,000 条轨迹和 164,003 个事件中没有不变量违规和确定性失败。这一验收针对隔离参考环境。任意新协议、按访客分配独立实例、跨实例处置和目标生产验收仍待完成。本地工作树未合并。

### 分支生命周期、事实硬校验与三组对照

在演示世界写满 200 个计数实体后验证首次超限拒绝。归档事务失败时，在线快照、归档和终端投影均保留原状态。正常归档保留一条有效版本票据，回收其余 77 个到期分支预算，同一主题重新发布得到新的事实标识。另做 8 轮发布与到期循环，累计发布并归档 480 个分支，峰值 182 个在线实体，结束回到 122 个。

Web 持续活动会续期，到期后返回基线，保留历史图供运营查询。模拟归档成功后账本未确认，重跑可补齐账本。备份工具将 4 个参考环境状态文件恢复到新目录，原基线 Web 路由与经鉴权的归档查询恢复可用。损坏备份会在写入恢复目录之前被拒绝。

发布器将结构化宿主、应用关系、引擎、版本和访问边界与 DWS 基线核对。使用错误宿主的正文、错误的结构化值以及接口字段里的未验证值均被测试拒绝。正确声明的值在 Web 和模拟接口中由程序渲染。真实 `deepseek-flash` 复验耗时 16.68 秒，从 3 个已有关联的实体锚点生成一条带结构化宿主声明的 GET 路径，激活后返回了核对通过的宿主字段。自由合成叙事仍经过语义复核。

同一个发布版本、6 个已知任务、2 个无关输入的合成对照结果如下。命中表示模拟访客跟随所提供的第一条核验路径后，HTTP 响应内容与预先标注的任务相符。

| 方式 | 已知任务命中 | 耗时中位数 | 无关输入误触发 |
|---|---:|---:|---:|
| 固定蜜饵 | 2/6 | 0.07 秒 | 0/2 |
| 规则适配 | 6/6 | 1.03 秒 | 0/2 |
| DeepSeek 适配 | 6/6 | 6.61 秒 | 0/2 |

DeepSeek 产生 12 次调用，供应商报告 38,494 个 token。试验未配置计价费率。结论适用于已知闭集任务的合成对照。真实访客后续探索率、目标生产容量和回切时间还需要现场验收。

最终 `deploy/check-v2.sh` 验收通过 417 项测试。两次基准回放逐字节一致，10,000 条轨迹及 164,003 个事件无不变量违规和确定性失败。网络、身份权限和跨实例处置尚未在目标生产环境验收。本轮修复位于本地工作树。

## 2026-11-23 发布连续性、图约束与模型态势研判

- 独立验证 Web 采集达到上限后已发布页面保持 HTTP 200，暂停新 Canary 签发，已有 Canary 仍能核验。模拟整点后采集恢复且事件序号保持唯一。模型预算耗尽的会话继续访问原内容，尚有场景配额的新会话仍可申请研判。
- 发布写入器分别调度文档、图、归档和生成任务。故障注入验证文档故障不会卡住其他发布任务，同一持续故障在第五次尝试后记录失败。测试将候选模拟路由替换为错误路径，独立访客监听器拒绝切换，旧场景继续提供服务。
- Web 记录按会话实际可见图识别路径和版本。新图限制 32 条边，每个节点最多 8 条关联边。测试覆盖入边与服务边合计超限以及边数恢复到上限可通过。SSH、MySQL 和模拟 AI 发布流程支持在已有 DWS 关系上选择两个锚点。
- Dashboard 支持按场景保存 Canary 路径并显示实际采集限额，浏览器验证非法路径反馈和保存后重新加载。旧 Canary 继续使用签发时的路径。实测 Web→模拟 AI→Web，确认各入口的根链可触发同一份跨协议态势快照。
- 真实 `deepseek-flash` 创建和审核了引用 3 个 DWS 锚点的 Web 场景，耗时 7.73 秒，模拟接口通过实际访客监听器检验。此前两次 Web 场景创建返回 502，复杂候选的独立诊断发现结构化字段和正文违反事实约束。
- 另启真实访客及控制监听器，提交合成的数据库备份查询。Web 队列完成后该会话收到 HTTP 200，决策为 `applied`。此次在线更新耗时 16.01 秒，发布 DWS v3 并更新 2 个页面。
- 对真实 Cowrie、MySQL 和模拟 AI 的合成请求进行独立复验。DeepSeek 将它们分别判为日志排查、目录探索和资料查询，约 1.37 秒、1.10 秒及 1.97 秒后能在原会话读到已发布蜜饵。每份引用 2 个 DWS 实体，新的 SSH 会话读不到已投放的日志。
- 真实 `deepseek-flash` 接收脱敏聚合态势，输出中风险和提交阻断复核建议。可回查 Web 和模拟 AI 的 Canary 证据。浏览器验证报告和链路跳转。测试拒绝仅以一次连接为依据的阻断复核建议。无信号不调用模型，失败保留已有观测。

最终 `deploy/check-v2.sh` 使用固定摘要的 Cowrie、MySQL、Nginx 和本轮重构建的受限 Cowrie，426 项全部通过，1 条既有 TestClient 弃用警告。两次回放逐字节一致，10,000 条轨迹、164,003 个事件中无不变量违规或确定性失败。专项用例再次核验了 Web→SSH 终止、不签发下一跳和异常态势建议回退。

目标生产 ACL、真实 VPC 流日志与代理、跨实例处置、长期负载和生产恢复尚未验收。态势告警建议在运营界面展示，外部通知和网关处置尚未接入。

## 2026-11-24 OA 真实行为生成与发布重试

旧版的真实模型演练中，自动创建首个场景成功，普通 OA 请求进入控制面后两次更新未通过。普通读取现在由服务器标记为数据库、资产、工单或知识库读取。模型只撰写相关说明，程序接入场景导航并校验既有图。审核只查看本次说明，发布器继续验证完整 DWS 图。图达到节点上限时，在当前会话复用一条已有页面路径承载新内容。

真实 `deepseek-flash` 在独立回环环境完成一次 OA 数据库读取到知识库投放的实测，原会话访问新页面返回 HTTP 200，另一个已登录会话直接访问返回 404。后续 Chromium 演练在同一个访客进程内依次点击数据库中心和资产列表，生成了不同的资料，分别发布到 DWS v3 和 v4。另一次 Chromium 演练未看到资产更新进入应用状态。模型输出和校验结果仍可能波动。

控制面在上一条发布尚未完成时保留同会话的新任务。发布器优先读取生成队列，空轮询使用延迟事务。故障测试用真实 SQLite 写入冲突阻塞提交，解除阻塞后在同一访客进程完成重试和投放。Dashboard 展示生成触发行为、校验失败原因，以及待发布、重试中和失败数量。

控制面可用 `RESEARCH_MODEL_TIMEOUT_SECONDS` 单独设置响应读取时限。用模拟 HTTP 传输验证了 180 秒配置。本轮调用的真实接口是 DeepSeek，目标环境的 GLM 及此前生产卡点仍需现场核对。

隔离环境全量复验通过 430 项。两次回放逐字节一致，10,000 条轨迹和 164,003 个事件均无不变量违规或确定性失败。SQLite 写锁恢复和待发布任务保留另做了专项复验。
