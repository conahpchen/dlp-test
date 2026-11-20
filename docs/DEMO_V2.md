# v2.1 演示与临时部署

## 准备和启动

使用 Linux、Docker、OpenSSH、OpenSSL、Chromium、Python 3.12 和 libseccomp2。安装 `requirements.txt` 后构建隔离 Cowrie 镜像。

```sh
docker build -f deploy/Dockerfile.cowrie-isolated -t dlp-cowrie-isolated:local .
```

选择私有临时目录作为 `HONEYNET_STATE_DIR`。设置 `HONEYNET_DWS_ATOMIC=1`、`HONEYNET_INGRESS_ONLY=1`，并为 `HONEYNET_COWRIE_EXPORT` 指定独立可读目录中的 `world.sqlite`。通过各进程的环境分别提供合成的 MySQL 密码、管理端密码和控制 token。真实模型凭据只给控制进程。逐个启动下列进程，默认绑定回环地址。

| 进程 | 命令 | 默认端口 |
|---|---|---|
| 访客、OA、假 AI | `deploy/run-v2-local.sh visitor` | 8080 |
| 控制及模型 | `deploy/run-v2-local.sh control` | 8081 |
| Dashboard | `deploy/run-v2-local.sh dashboard` | 8082 |
| MySQL L2 | `deploy/run-v2-local.sh mysql` | 3307 |
| Cowrie | `deploy/run-v2-local.sh cowrie` | 2222 |

先访问访客 `/healthz` 初始化 DWS，再启动依赖 checkpoint 的 MySQL 和 Cowrie。Dashboard 所在进程可显式设置 `HONEYNET_VISITOR_PORT=8080 HONEYNET_COWRIE_PORT=2222 HONEYNET_MYSQL_PORT=3307 HONEYNET_DASHBOARD_PORT=8082`，使「部署与运行」发起对应只读探针。

## 演示流程

普通访客访问 `/` 时先看到预置 OA。登录演示账号后，可以通过知识库查看本会话已生成的「相关资料」。分离部署时在 visitor 和 control 进程同时设置 `RESEARCH_ASYNC_ADAPTATION=1`，由 control 处理模型任务。任务完成后重新打开知识库可看到通过校验的线索。单独演示场景可直接打开创建结果中的 `/decoy/{id}/index`。

1. 使用 `operator` 登录 Dashboard。「部署与运行」展示实际监听器响应、当前发布、DWS 变更、HTTP p95 和模型预算。未配置或未采样时对应位置显示缺失状态。
2. 在「场景模板」加载预置模板，保存新修订。从控制接口创建场景时传入该模板的 ID 和修订号。
3. 在「数据发布」打开合成知识库文档草稿，审批后读取已发布版本，演示撤回。
4. 在「会话证据」用真实访问产生 Web 时间线、Agent 声明和 Canary 凭据命中。同时查看发布清单与依赖检查，演示回滚。
5. 演示 L3 时按照迁移说明配置固定摘要镜像。Nginx 文档投影和完整 MySQL 通过各自的 Unix socket 验证。

## 临时 HTTPS 外部入口

授权主机上的 Nginx 可安装 `deploy/harden/nginx-external.conf`。临时配置监听 `127.0.0.1:8443`，上游固定为访客 `127.0.0.1:8080`，证书和私钥由操作者挂载到 `/edge`。生产域名、对外绑定地址、TLS 证书、入站防火墙和 SSH 访问路径按实际网关确定。可通过受控 SSH 隧道访问 Dashboard 回环端口。公开 HTTPS 白名单仅包括 OA、假 AI 和诱饵路由。

上线前从指定入口验证合法 OA 和假 AI 响应，并逐项验证公开路径无管理或控制接入，再单独用合成凭据探测 SSH 和 MySQL 的握手。L3 Unix socket 保持本机。外部验证无需扫描目标端口或写入业务数据。

## 本机联调验收

设置以下四个变量为已存在于本机、由操作者审核的镜像后，运行 `deploy/check-v2.sh`。

- `HONEYNET_TEST_COWRIE_IMAGE`
- `HONEYNET_TEST_COWRIE_ISOLATED_IMAGE`
- `HONEYNET_TEST_L3_MYSQL_IMAGE`
- `HONEYNET_TEST_L3_HTTP_IMAGE`

脚本运行启用真实容器的套件、逐字节回放和 10,000 条轨迹。验收记录见 [PROGRESS_V2.md](PROGRESS_V2.md)。

[实时运行页面截图](screenshots/operations-v2-runtime.png)来自启用临时 HTTP、假 AI 和 Dashboard 的本机实例。SSH、MySQL 和两种 L3 服务的证据在各协议的联调记录中。

## 自动生成与投放

需要自动模式时，给 visitor 和 control 同时设置 `RESEARCH_AUTOMATIC_DECOYS=1`，并在 control 配置模型。没有激活场景时，control 在 DWS 就绪后自动生成、复核并激活初始 HTTP 场景。访客从预置 OA 开始。成功读取知识库、数据库目录、资产或工单时触发后台任务，模型根据本会话的近期事件判断意图并提出内容和模拟 HTTP 接口。校验通过后，访客会话可以在再次打开知识库时看到相关线索。日常流程无需手填模板、生成目标或逐次审批。Web 入口可在现有蜜网内生成页面、线索和模拟 HTTP 服务。

同一开关也适用于 MySQL 和 Cowrie 的启动进程，以及 Cowrie 的 `cowrie-events` 采集进程。Cowrie 镜像需要用更新后的代码重新构建。控制进程使用同一份研究数据库，SSH 额外通过现有只读挂载的导出目录交付合成配置。模拟 AI API 由 visitor 提供。

| 入口 | 行为信号 | 经校验后的投放 |
| --- | --- | --- |
| SSH | 已连接终端里查看文件、目录或配置 | 当前会话虚拟文件系统内的 `/etc/service-reference.conf` 或 `/var/log/service-reference.log` |
| MySQL | 已认证连接查询模拟目录 | 当前连接后续同类查询的附加合成目录记录 |
| 模拟 AI API | 已认证聊天请求的活动类别 | 同一凭据和客户端会话标签的后续回复 |

控制进程推理使用有限的行为分类，不接收原始终端命令、SQL、聊天文本或认证材料。模型输出限于短文本和结构化字段，投放在现有隔离入口。不同协议分别使用自己的会话键，现有版本票据和 Canary 仍提供各自的关联能力。

### 多入口演示顺序

- **SSH 配置查找**。在同一终端先读普通文件，再查看配置相关路径。模型明确判断为配置查找时，系统在该会话的只读配置文件中附加服务端签发的合成账号和口令占位值。用同一会话重新列目录、读取文件，再用另一终端确认该文件未共享。占位值没有被注册到认证服务。
- **SSH 日志查找**。在同一终端查阅日志目录或日志文件。模型判断为日志排查后，可以在原会话生成只读日志蜜饵 `/var/log/service-reference.log`；原配置蜜饵仍可读取。
- **模拟 AI 文档查找**。在已认证的模拟 AI API 会话询问运维文档，等待后台任务，再比较该会话和新会话的回复。
- **MySQL 目录探索**。先查询原有目录，后台根据行为生成合成条目，再在原连接查询并与新连接对照。
- **Web 动态模拟服务**。在同一 Web 场景中发起受支持的组件查询，查看 AI 是否生成对应的模拟 HTTP 路由和结构化响应。通过独立会话确认该会话新增的路由未共享。
