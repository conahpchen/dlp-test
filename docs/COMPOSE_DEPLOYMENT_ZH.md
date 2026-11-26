# Docker Compose 部署

本手册用于在一台 Linux 主机上启动 Web/OA、模拟 AI API、模拟 MySQL、Cowrie SSH、DWS、控制面和 Dashboard。控制面和访客服务使用独立的容器网络。Dashboard 在宿主机上仅绑定回环地址，控制 API 仅在容器管理网监听。

## 1. 启动

准备 Docker Engine 和 Docker Compose V2。部署文件使用命名卷子路径挂载和 `up --wait`，已使用 Engine 29.9.2 和 Compose 2.40.3 验证。

```bash
cp deploy/docker/.env.example deploy/docker/.env
chmod 600 deploy/docker/.env
```

在 `deploy/docker/.env` 为 `HONEYNET_DASHBOARD_PASSWORD`、`HONEYNET_CONTROL_TOKEN` 和 `HONEYNET_MYSQL_PASSWORD` 填写三个独立值。然后从仓库根目录运行以下命令。

```bash
docker compose --env-file deploy/docker/.env -f deploy/docker/compose.yaml up --build --wait
docker compose --env-file deploy/docker/.env -f deploy/docker/compose.yaml ps
```

| 入口 | 默认位置 |
| --- | --- |
| OA 与模拟 AI API | `http://127.0.0.1:8080/` |
| Cowrie SSH | `127.0.0.1:2222` |
| 模拟 MySQL | `127.0.0.1:3307` |
| Dashboard | `http://127.0.0.1:8082/`，用户名 `operator` |

对接访客网时，将 `HONEYNET_PUBLIC_BIND` 设为宿主机的访客网地址，并在防火墙上仅放行所需访客端口。远程运维通过管理网中的 SSH 隧道访问 Dashboard。Compose 使用持久化卷存储 DWS、Cowrie 投影和日志，更新服务前按部署实施手册备份状态。

## 2. 启用可选功能

- **真实模型与自动生成**。填写 `RESEARCH_MODEL_BASE_URL`、`RESEARCH_MODEL` 和 `RESEARCH_MODEL_API_KEY`，然后设置 `RESEARCH_AUTOMATIC_DECOYS=1` 并重新应用 Compose 配置。真实模型密钥仅进入控制容器。模型超时、推理和 token 预算参数见环境变量示例。
- **合成员工**。提供合成员工凭据、Cowrie 账号文件及已核验的 SSH 主机公钥文件，启动时追加 `-f deploy/docker/compose.employees.yaml --profile employees`。Cowrie 账号和 SSH 主机公钥文件的变量名见该配置。
- **数据发布**。DWS 发布 worker 随 Web 容器启动。场景激活时，控制面通过内部探测服务核验访客路由，发布状态和记录可在 Dashboard 的「场景发布」查看。此 Compose 配置提供固定的访客协议入口及其内置场景能力。

## 3. 验收

1. 用浏览器打开访客首页和 Dashboard，并确认 Dashboard 会要求登录。
2. 检查 Cowrie 的 SSH 握手、模拟 MySQL 的目录查询以及模拟 AI API 的模型列表和对话响应。
3. 在 Dashboard 激活一个经过校验的候选场景，确认 DWS 发布完成，访客网关能返回场景内容。
4. 用同一访客地址请求 `/api/research/status` 和 `/api/evidence/runs`，两个请求均应返回 404。

本配置的默认端口只能从宿主机访问。正式开放访客端口前，按目标环境复验管理网隔离、Docker 出站策略、SELinux 和真实模型连通性。

本次在 Linux 虚拟环境中启动了基础 Compose。实测通过 Web 与模拟 AI API 交互、Cowrie SSH 握手、MySQL 目录查询，以及 Dashboard 鉴权和访客入口隔离。用确定性模型生成候选场景，实测通过 DWS 发布和模拟接口访问。MySQL 停止时，依赖该服务的新场景被拒绝激活。已激活场景所依赖的 MySQL 停止后，控制面自动恢复上一场景。
