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
