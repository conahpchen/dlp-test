# 本地部署与迁移

1. 安装 `requirements.txt`，使用 Python 3.12 创建 `.venv`。为实例设置独立的 `HONEYNET_STATE_DIR`，默认 `var/v2`，仅一个访客进程写 DWS checkpoint。
2. 运行 `deploy/run-v2-local.sh visitor`。通过 `curl -fsS http://127.0.0.1:8080/v1/models` 初始化 checkpoint。
3. 通过环境提供 `HONEYNET_CONTROL_TOKEN`、可选真实模型配置，另启 `deploy/run-v2-local.sh control`。仅该进程需要真实模型凭据。访客进程关闭自动在线推理，模型适配可通过授权控制请求触发。
4. 提供独立的 `HONEYNET_DASHBOARD_PASSWORD`，另启 `deploy/run-v2-local.sh dashboard`。浏览器访问 `http://127.0.0.1:8082/`，使用用户 `operator`。在「场景模板」中可新增或修订展示方式和生成目标。创建研究 run 时，向控制面的 `POST /api/research/runs` 传入 `template_id` 和 `template_revision`。模板修订影响以该修订创建的 run；已运行场景保留其来源版本。
5. 提供独立合成密码 `HONEYNET_MYSQL_PASSWORD`，另启 `deploy/run-v2-local.sh mysql`。用 MySQL 客户端连接 `127.0.0.1:3307`，账号 `catalog_reader`。支持的目录查询见架构文档。

端口可由对应 `HONEYNET_*_PORT` 环境变量覆盖。全部监听器默认仅本机可达。控制面和 Dashboard 不应通过访客反向代理转发。当前配置为本地验收用途，进程共享状态目录，完整网络与权限隔离仍待验证。

Schema 1 checkpoint 可直接读取。升级后首次保存写 Schema 2，新增会话绑定和自写版本。升级前保留 checkpoint 和 Research SQLite 的一致性备份。回退代码时恢复备份。Research 表增量创建，旧事件历史保留。

新 checkpoint 包含私有写入版本索引。旧 checkpoint 可读取，旧格式中缺少该索引的历史写入保留原先的可见性。
