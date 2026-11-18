# AI 蜜网部署实施手册

本手册用于 Linux 上的临时部署、演示与目标环境复验。参考验证对应实现提交 `2d7647e`。实际部署时记录采用的完整提交 SHA、镜像摘要、部署人和时间。生产外部入口按目标网络配置逐项验收。

## 1. 准备环境

准备 Linux、Python 3.12、Docker、OpenSSH、OpenSSL 和 libseccomp2。浏览器演示可使用 Chromium。部署前确认运行账号、Docker 权限、持久化路径、端口占用及存储容量。在已有部署上升级前，先停止写入并保存状态和当前配置。

```sh
git clone --branch codex/v2-delivery https://github.com/conahpchen/dlp-test.git
cd dlp-test
git rev-parse HEAD
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
docker build -f deploy/Dockerfile.cowrie-isolated -t dlp-cowrie-isolated:local .
```

## 2. 配置并启动监听器

每个启动进程使用一致的状态路径和投影路径。首次部署时创建专用目录。从服务管理器或密钥管理系统为各进程单独注入凭据。

```sh
umask 077
export HONEYNET_STATE_DIR="${PWD}/var/v2-state"
export HONEYNET_COWRIE_EXPORT="${PWD}/var/v2-projection/world.sqlite"
export HONEYNET_DWS_ATOMIC=1
export HONEYNET_INGRESS_ONLY=1
install -d -m 0700 "$HONEYNET_STATE_DIR" "$(dirname "$HONEYNET_COWRIE_EXPORT")"
```

| 启动顺序 | 命令 | 回环端口 | 启动条件 |
| --- | --- | --- | --- |
| 1 | `deploy/run-v2-local.sh visitor` | 8080 | 启动后执行 `curl -fsS http://127.0.0.1:8080/healthz`，初始化 DWS。 |
| 2 | `deploy/run-v2-local.sh mysql` | 3307 | 设置合成的 `HONEYNET_MYSQL_PASSWORD`，确认 DWS 已就绪。 |
| 3 | `deploy/run-v2-local.sh cowrie` | 2222 | 确认投影文件已生成，容器以只读方式挂载独立投影目录。 |
| 4 | `deploy/run-v2-local.sh control` | 8081 | 设置 `HONEYNET_CONTROL_TOKEN` 和 `HONEYNET_RELEASE_MONITOR=1`。使用真实模型时，只在该进程配置 `RESEARCH_MODEL_API_KEY`、`RESEARCH_MODEL_BASE_URL` 和 `RESEARCH_MODEL`。 |
| 5 | `deploy/run-v2-local.sh dashboard` | 8082 | 设置 `HONEYNET_DASHBOARD_PASSWORD`，以 `operator` 登录。配置实际探测端口。 |

启动器以前台方式运行。演示可在独立终端操作，持久运行时由服务管理器分别托管。自定义端口时同步更新 Dashboard 的 `HONEYNET_VISITOR_PORT`、`HONEYNET_COWRIE_PORT`、`HONEYNET_MYSQL_PORT` 和 `HONEYNET_DASHBOARD_PORT`，以及控制面的访客健康探测配置。[启动器](../deploy/run-v2-local.sh)

## 3. 配置演示入口

- **管理端**通过运维 SSH 隧道访问。在操作员电脑上执行 `ssh -N -L 8082:127.0.0.1:8082 ops@DEPLOY_HOST`，然后访问 `http://127.0.0.1:8082/`。本地端口被占用时调整 `-L` 的第一个端口。
- **访客 HTTPS**使用仓库的 Nginx 白名单配置。参考配置绑定 `127.0.0.1:8443`，转发到 `127.0.0.1:8080`。为 Nginx 配置 `/edge` 下的证书和私钥，核对上游，使用 `nginx -t` 检查生效配置后再启动或重载。目标环境需确认实际域名、绑定地址、TLS 和入站 ACL。[网关配置](../deploy/harden/nginx-external.conf)
- **Cowrie 和 MySQL**默认只监听回环。临时演示从部署主机或受控隧道发起。目标环境如需开放，按已批准的源网段配置入口，并验证允许与拒绝两种网络路径。
- **L3 MySQL 和 Nginx HTTP**按仓库的运行时配置使用批准的固定摘要镜像，保留本机 Unix socket 边界。[迁移与运行时说明](MIGRATION_PLAN.md)

## 4. 执行验收并留证

在独立的联调状态目录中备好审核通过的四个本地镜像，分别设置 `HONEYNET_TEST_COWRIE_IMAGE`、`HONEYNET_TEST_COWRIE_ISOLATED_IMAGE`、`HONEYNET_TEST_L3_MYSQL_IMAGE` 和 `HONEYNET_TEST_L3_HTTP_IMAGE`，再执行 `deploy/check-v2.sh` 并保留退出码和输出。镜像与运行要求见 [临时部署验证说明](DEMO_V2.md)。

| 验收项 | 操作及预期 |
| --- | --- |
| Web 与模拟 AI | 从批准的入口请求 OA 和模拟 AI，核对应用响应；验证公开网关对管理、控制、审计和内部健康路径返回 404。 |
| Cowrie SSH 与 MySQL | 使用合成凭据建立真实连接，执行读取或查询操作，记录协议结果。 |
| Canary 与版本绑定 | 建立两个独立会话，核对诱饵唯一性。按联调用例在四个入口复用同一张受控票据，核对版本、交付凭据和命中事件。 |
| Dashboard | 触发实际访问，核对请求延迟、监听器、场景及会话证据反映本次操作；试走模板修订和数据审批。 |
| 模型与回退 | 在临时站点生成候选场景，确认校验、发布、访客访问和回滚记录。在隔离联调环境停掉已批准的依赖，核对补偿和旧会话行为。 |
| 目标环境隔离 | 从允许及拒绝的网络位置做非破坏性连接验证。核对管理与控制监听、访客出站、模型凭据作用域和 L3 端口暴露。 |

保存提交 SHA、镜像摘要、测试时间、来源网段、响应结果和脱敏证据。参考环境记录的 396 项通过来自 `deploy/check-v2.sh`，目标站点需填写自己的结果。[历史执行记录](PROGRESS_V2.md)

## 5. 回退与恢复

1. 场景异常时，通过已鉴权的管理流程选择上一个可用发布版本，核对新会话所用版本和已有会话绑定。
2. 外部入口配置异常时，在网关撤下本次入口变更并保留运维通道。恢复上一个通过校验的网关配置后复测合法与受限路径。
3. 需要回退应用时，先停写并保存当前状态和日志，再恢复已验收的代码、镜像与对应状态备份。使用 SQLite 一致性备份，或在所有写入者停止后按恢复流程保存状态目录。重启后重跑协议、隔离和 Dashboard 冒烟检查。

整体验收以目标站点的检查结果和 [新版验收说明](ACCEPTANCE_V2.md) 为依据。生产网络、证书、账号权限及 [需求追踪表](REQUIREMENTS_TRACEABILITY.md) 中尚未完成的条目应在发布记录中具体列出。
