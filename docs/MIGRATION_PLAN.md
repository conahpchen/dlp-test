# 本地部署与迁移

1. 安装 `requirements.txt`，使用 Python 3.12 创建 `.venv`。为实例设置独立的 `HONEYNET_STATE_DIR`，默认 `var/v2`，仅一个访客进程写 DWS checkpoint。
2. 运行 `deploy/run-v2-local.sh visitor`。通过 `curl -fsS http://127.0.0.1:8080/v1/models` 初始化 checkpoint。
3. 通过环境提供 `HONEYNET_CONTROL_TOKEN`、可选真实模型配置，另启 `deploy/run-v2-local.sh control`。仅该进程需要真实模型凭据。访客进程关闭自动在线推理，模型适配可通过授权控制请求触发。
4. 提供独立的 `HONEYNET_DASHBOARD_PASSWORD`，另启 `deploy/run-v2-local.sh dashboard`。浏览器访问 `http://127.0.0.1:8082/`，使用用户 `operator`。在「场景模板」中可新增或修订展示方式和生成目标。创建研究 run 时，向控制面的 `POST /api/research/runs` 传入 `template_id` 和 `template_revision`。模板修订影响以该修订创建的 run；已运行场景保留其来源版本。
5. 提供独立合成密码 `HONEYNET_MYSQL_PASSWORD`，另启 `deploy/run-v2-local.sh mysql`。用 MySQL 客户端连接 `127.0.0.1:3307`，账号 `catalog_reader`。支持的目录查询见架构文档。

端口可由对应 `HONEYNET_*_PORT` 环境变量覆盖。全部监听器默认仅本机可达。控制面和 Dashboard 不应通过访客反向代理转发。当前配置为本地验收用途，进程共享状态目录，完整网络与权限隔离仍待验证。

Schema 1 checkpoint 可直接读取。升级后首次保存写 Schema 2，新增会话绑定和自写版本。升级前保留 checkpoint 和 Research SQLite 的一致性备份。回退代码时恢复备份。Research 表增量创建，旧事件历史保留。

新 checkpoint 包含私有写入版本索引。旧 checkpoint 可读取，旧格式中缺少该索引的历史写入保留原先的可见性。

如需本地 MySQL Canary 联调，在访客及 MySQL 进程启动前均设置 `HONEYNET_ENABLE_MYSQL_CANARY=1`。已发送的 HTTP 诱饵会附带合成 MySQL 核验账号；正确协议证明写入使用证据，账号仍限制在 Web 核验范围。正常 MySQL 目录账号独立。

新签发的 HTTP 凭据有效期为 24 小时，历史凭据按已有发送时间迁移，缺少发送时间时从迁移时起计算。授权控制面可用 `GET /api/research/runs/{run_id}/canaries` 查看状态，用 `POST /api/research/canaries/{credential_id}/revoke` 撤销。历史命中会保留。

Cowrie 的本地采集器可用 `python -m entries.cowrie.bridge --in /path/to/cowrie.json --out /path/to/events.jsonl --canary-state var/v2/research.sqlite`。桥接器读取原始传感器文件并对密码做匹配，统一导出保留哈希。Cowrie 自己的原始日志按敏感证据管理。此命令适用于拥有共享账本权限的本机桥接进程。

经授权的控制面可以从 `GET /api/research/runs/{run_id}/canary-uses` 读取证据关系，并通过 `POST /api/research/canary-uses/{use_id}/revisions` 追加带原因的判断，写入当前 `revision`、`associated` 和 `reason`。原始使用证据与旧修订保留。


### 启动 DWS 投影版 Cowrie

从仓库根目录运行：

```sh
docker build -f deploy/Dockerfile.cowrie-dws -t dlp-cowrie-dws:local .
mkdir -p var/v2-cowrie
chmod 755 var/v2-cowrie
export HONEYNET_COWRIE_EXPORT="$PWD/var/v2-cowrie/world.json"
export HONEYNET_COWRIE_HOST=web01
BUSINESS_STATE_FILE="$PWD/var/v2/world.json" .venv/bin/python -m entries.cowrie.projection \
  --out "$HONEYNET_COWRIE_EXPORT" --host "$HONEYNET_COWRIE_HOST"
deploy/run-v2-local.sh cowrie
```

已有的 visitor 单写进程可以在相同环境下启动，使它在 checkpoint 更新后原子替换专用投影文件。容器只需挂载该专用目录，文件路径与 checkpoint 分离。现存终端继续使用加载的投影；新终端载入替换后的文件。如果投影导出失败，旧的有效投影文件仍可读取，可通过上面的导出命令从 checkpoint 修复。checkpoint 与各协议投影之间的统一发布事务仍待建设。


### 本地原子 DWS 发布模式

新实例在启动所有入口前设置 `HONEYNET_DWS_ATOMIC=1`，将 `HONEYNET_COWRIE_EXPORT` 指向专用目录中的 `world.sqlite`，目录权限须允许 Cowrie 容器的 UID 999 读取。保留 checkpoint 目录的私有权限。两目录必须位于同一台主机的同一文件系统。依次启动 visitor 并访问其 `/v1/models` 初始化已提交快照，再启动 control、dashboard、mysql 和 cowrie。SQLite 模式直接初始化 Cowrie 导出，省掉上面手工 JSON 导出步骤。

老 JSON 实例照常读取。使用单独的状态目录可以演练 SQLite 模式。SQLite 事务覆盖本机 DWS checkpoint 和 Cowrie 投影。Web 场景活动状态、外部服务启停和跨协议会话关联仍是独立状态。


在原子模式的 Dashboard 打开「数据发布」。可载入或新建 `/kb/*.md` 中的合成常规文档，保存后审批发布。发布 worker 由单写入 visitor 进程运行，成功提交后才显示已发布。基线文档在编辑期间发生变化时，旧草稿会被拒绝。可追加撤回版本，读取原来版本的 Web 和 SSH 会话保留其事实。该页面当前支持知识库文档，协议和服务级发布还需进一步接入。


### 模型预算与后台适配

模型默认每日允许预留 1,000,000 token，由共享 SQLite 账本 `RESEARCH_MODEL_BUDGET_FILE` 保存。未配置时，账本位于 `RESEARCH_STATE_FILE` 同目录。可设置 `RESEARCH_MODEL_DAILY_TOKENS`。费用上限通过 `RESEARCH_MODEL_DAILY_COST_MICROUSD` 配置，非零时需同时设置 `RESEARCH_MODEL_INPUT_MICROUSD_PER_MILLION` 和 `RESEARCH_MODEL_OUTPUT_MICROUSD_PER_MILLION`。成本按输入的保守估计与最大输出预留，避免依赖供应商返回计量；该估算不等同实际账单。UTC 日切换后使用新额度。

控制面共享短时精确请求缓存与并发限制。供应商连续三次失败时进入 30 秒冷却，恢复时放行一个探测。将 `RESEARCH_ASYNC_ADAPTATION=1` 同时配置给 visitor 和 control 可启用后台适配。访客只对已分类活动提交有界任务，控制面处理队列。POST `/interact` 可返回 202，随后在同一 Cookie 会话 GET 同一路径。原有场景页面在排队或模型拒绝期间可继续访问。


管理端「会话证据」可按实际研究 run 查看 Web 事件时间线、Agent 登记和 Canary 关联历史。含核验链接的诱饵响应提供可选登记入口，接收固定枚举的运行类型、框架、模型系列和任务类别。只接受收到过有效诱饵的来源会话，重复提交相同声明返回原编号。登记由客户端自报，行为假设及置信度单独显示。


在「会话证据」选择模型生成的场景可审批激活或回滚。发布前编译现有 HTTP 监听器的有限路由。场景如声明 `mysql_catalog`，执行激活的控制面或 Dashboard 进程需配置 `HONEYNET_MYSQL_HEALTH_PASSWORD`，其值为本地合成目录密码，并与 MySQL 监听器使用同一个 `HONEYNET_MYSQL_PORT`。健康检查的地址固定为 loopback，认证后必须读到目录。缺失、认证失败或未批准的独立运行时会拒绝激活，已发布场景继续响应。目录密码只交给执行授权验证的进程。


### 版本绑定、依赖补偿和入口隔离

- 操作员可通过 `POST /api/evidence/runs/{run_id}/bindings` 为已有发布签发一小时的版本选择票据。Web 根入口和集成假 AI API 接收 `X-Honeynet-Binding`，MySQL 使用返回的合成用户名和既有目录密码。票据不替代协议身份验证。已建立会话维持原来的版本。若配置了 Cowrie 专用投影目录，签发时同时导出终端安全的历史快照，并返回 `ssh_environment=HONEYNET_BINDING`。登录时在可信客户端设置这个单独的票据变量，并添加 OpenSSH 参数 `-o SendEnv=HONEYNET_BINDING`，SSH 保持独立认证。
- 控制面 `POST /api/research/active/reconcile` 复查当前发布的知识库、HTTP 监听器和已声明的本地 MySQL 依赖。设置 `HONEYNET_RELEASE_MONITOR=1` 后控制进程每 5 秒检查，同一故障连续两次触发补偿。不可用时恢复健康的前一发布，若不存在则停止向新访客投放；已有访客保持其原场景。
- Linux 机器安装 `libseccomp.so.2` 后，可为 visitor 和 mysql 启动设置 `HONEYNET_INGRESS_ONLY=1`。这两个进程的出站连接和数据报操作被系统调用阻断，正常回环入口仍可接入。visitor 使用本地 DWS 而非直连 Postgres。Cowrie 可使用同一入站模式。先构建 `docker build -f deploy/Dockerfile.cowrie-isolated -t dlp-cowrie-isolated:local .`，再以 `HONEYNET_INGRESS_ONLY=1 deploy/run-v2-local.sh cowrie` 启动。该镜像关闭 SSH 转发与 SFTP，并限制出站连接。管理面和控制面仍使用独立的回环监听器。外部反向代理、宿主账号及持久目录权限需要按目标部署验证。


### L3 独立 MySQL

在能访问本地受控 Docker daemon 的管理与控制进程配置 `HONEYNET_L3_MYSQL_IMAGE` 为已经拉取并审核的 `mysql@sha256:…`，并配置一个短路径私有目录 `HONEYNET_L3_STATE_DIR`，目录权限 0700。模型可以请求 `isolated_instance`，仍须给出确定的 SQL 需求并由操作员显式批准。控制 API 使用 `POST /api/research/runs/{run_id}/activate?approve_isolated=true`，管理端提供独立复选框。

运行时最多一个实例。容器无网络、无宿主端口、使用只读根文件系统和临时数据目录，CPU、内存与进程数受限。初始化只读取已绑定版本的 DWS 文档路径、事实标识和版本。获授权的本机操作者可使用该目录下 `{run_id}/socket/mysql.sock` 和私有 `{run_id}/catalog.password`，账号为 `catalog_reader`。普通访客的 Web 和 SSH 版本绑定不受此账号控制。

回滚或故障补偿从投放中撤回该实例并清理容器、数据卷和临时文件。若上一次已切到另一发布，可用 `POST /api/research/runs/{run_id}/release/retire` 清理非活跃的暂留实例。现场网关及其他协议的 L3 能力需独立配置、审批和验证。


独立 HTTP L3 使用 `HONEYNET_L3_HTTP_IMAGE=nginx@sha256:…`，场景声明 `isolated_instance` 与 `isolated_reason=standalone_http`，以相同的操作员审批流程激活。只读 DWS 文档投影经本机 `{run_id}/socket/http.sock` 提供。HTTP 与 MySQL L3 共用一个运行额度。独立 HTTP 不经访客 HTTPS 网关转发。

临时 HTTPS、Dashboard 演示及本机入口验收命令见 [DEMO_V2.md](DEMO_V2.md)。
