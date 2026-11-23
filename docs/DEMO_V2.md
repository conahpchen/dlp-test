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

## 员工 Agent 的独立业务活动

各入口按自己的排班运行员工 Agent。六个身份用于区分岗位和真实协议观测。SSH 巡检、数据库复核和模拟 AI 咨询独立执行。OA 如需员工活动，只安排一名业务员工检查待办队列。旧的 OA 流转可作为单独的历史演示。

| 入口 | 员工 | 已接入的活动 |
|---|---|---|
| Cowrie SSH | 运维 `ops01`、开发 `dev01` | 查看模拟主机名、查看发布交接笔记 |
| MySQL | DBA `dba01`、测试 `test01` | 查询合成数据库目录、检查连通性 |
| 模拟 AI API | 业务 `biz01`、经理 `manager01` | 查询运维手册、复核服务资产 |
| OA 工单（可选） | 业务 `biz01` | 登录后查看已有待处理工单数量 |

给运行对应入口及员工进程的环境分别配置下列合成凭据。员工进程只连接配置端口的 `127.0.0.1`，不会从任何页面或模型输出提取目标地址。

- SSH 为 `ops01` 和 `dev01` 提供 `HONEYNET_EMPLOYEE_SSH_OPS01_SECRET` 和 `HONEYNET_EMPLOYEE_SSH_DEV01_SECRET`，将对应用户和口令写入 Cowrie 私有 `userdb.txt`，用 `HONEYNET_EMPLOYEE_COWRIE_USERDB` 挂载。文件按 Cowrie 格式使用 `用户名:x:口令`，并保留环境已有的演练账号。员工进程需设置 `HONEYNET_EMPLOYEE_SSH_KNOWN_HOSTS` 指向已核验主机密钥的文件。采集进程 `cowrie-events` 使用相同的 SSH 员工凭据识别成功认证的会话。
- MySQL 服务与员工进程使用 `HONEYNET_EMPLOYEE_MYSQL_DBA01_SECRET` 和 `HONEYNET_EMPLOYEE_MYSQL_TEST01_SECRET`。`catalog_reader` 公共模拟账号仍由该服务自身配置。
- 模拟 AI API 与员工进程共享 `HONEYNET_EMPLOYEE_LLM_BIZ01_SECRET` 和 `HONEYNET_EMPLOYEE_LLM_MANAGER01_SECRET` 两个独立签名密钥，每个至少 16 字符。员工进程另设 `HONEYNET_EMPLOYEE_FAKE_API_TOKEN` 为有效的模拟服务 token。
- 启用单人 OA 时，访客服务与员工进程共享 `HONEYNET_EMPLOYEE_WEB_BIZ01_SECRET`（至少 16 字符），并仅给员工进程提供 `HONEYNET_EMPLOYEE_WEB_BIZ01_PASSWORD` 作为现有模拟账号的密码。用 `deploy/run-v2-local.sh employees --service web` 单独启动 OA 任务。

默认每 15 分钟按员工排班执行一次，默认时区为 `Asia/Shanghai`，可用 `HONEYNET_EMPLOYEE_TIMEZONE` 指定时区。用 `deploy/run-v2-local.sh employees` 调度全部六人，或为每个入口分别启动一个员工进程：

```sh
deploy/run-v2-local.sh employees --service ssh
deploy/run-v2-local.sh employees --service mysql
deploy/run-v2-local.sh employees --service llm
```

验收时用 `deploy/run-v2-local.sh employees --once --at 10:30` 运行一次已配置任务。Dashboard 的「部署与运行 → 员工 Agent 跨服务活动」展示实际请求完成、请求失败或缺少凭据，和访客认证告警分开查询。SSH 和 MySQL 的未认证连接探测仍保留为普通入口观测。

## 自动生成态势报告

启动控制面时设置 `HONEYNET_SITUATION_REPORTS=1`。控制面每 15 秒读取当前保留的观测，每小时写入一次报告。同一枚已经交付的 Canary 在一小时窗口内被两个及以上协议入口记录为有效关联时，额外生成一份报告。报告保留在状态目录的 `situations.sqlite`，在管理端「态势报告」查看和打开链路证据。未接通的数据源显示为尚未就绪。报告按观察和证据计数，访客身份归因需要额外证据。

## SSH 流日志和证据处置

在 `cowrie-events` 启动环境中设置 `HONEYNET_FLOW_INTERFACE` 为与该采集点对应的网卡标识。采集进程和导入进程使用同一份至少 32 字节的 `HONEYNET_FLOW_HMAC_KEY`，由部署环境提供。Cowrie 日志和待导入流日志必须处于同一网络观测点，同一连接在这两个来源应使用相同的源/目标 IP、端口和协议。

```sh
deploy/run-v2-local.sh flow-import /path/to/default-vpc-flow-log.txt
```

导入支持 AWS VPC Flow Logs 默认版本 2 的空格分隔文本，可包含列名。仅 `ACCEPT OK` 的 TCP 记录进入匹配，重复记录去重，非采集数据和不符合条件的记录分别计数。格式无效时整个批次拒绝。Dashboard「攻击链路」在相同网卡、精确五元组且时间窗内只有一个 SSH 会话时显示匹配，使用哈希存储地址和端口组合，显示接口、包数、字节数及时间。NAT、反向代理和不同观测点可能改变五元组，这些场景需要各自的关联证据。

在 Dashboard 进程中设置 `HONEYNET_DASHBOARD_APPROVER_PASSWORD`。使用 `operator` 从「攻击链路」申请撤销或恢复关联，或从「Canary 凭据」申请撤销标记。使用另一浏览器会话，以 `reviewer` 登录 `/responses` 审核。申请和审核都记录说明及时间。审批与实际状态更新一起写入账本，版本冲突保留原有最新状态并记录冲突。凭据撤销后保留历史证据，可用新标记开启后续演练。控制面的既有令牌保护接口作为独立管理路径。

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
| 模拟 AI API | 已认证聊天请求的活动类别 | 同一客户端观测会话的后续回复，客户端需要保留服务签发的会话 cookie |

控制进程推理使用有限的行为分类，不接收原始终端命令、SQL、聊天文本或认证材料。模型输出限于短文本和结构化字段，投放在现有隔离入口。不同协议分别使用自己的会话键，现有版本票据和 Canary 仍提供各自的关联能力。

### 多入口演示顺序

- **SSH 配置查找**。在同一终端先读普通文件，再查看配置相关路径。模型明确判断为配置查找时，系统在该会话的只读配置文件中附加服务端签发的合成账号和口令占位值。用同一会话重新列目录、读取文件，再用另一终端确认该文件未共享。占位值没有被注册到认证服务。
- **SSH 日志查找**。在同一终端查阅日志目录或日志文件。模型判断为日志排查后，可以在原会话生成只读日志蜜饵 `/var/log/service-reference.log`；原配置蜜饵仍可读取。
- **模拟 AI 文档查找**。在已认证的模拟 AI API 会话询问运维文档，等待后台任务，再比较该会话和新会话的回复。
- **MySQL 目录探索**。先查询原有目录，后台根据行为生成合成条目，再在原连接查询并与新连接对照。
- **Web 动态模拟服务**。在同一 Web 场景中发起受支持的组件查询，查看 AI 是否生成对应的模拟 HTTP 路由和结构化响应。通过独立会话确认该会话新增的路由未共享。
## DWS 图发布与协议会话分支

在使用 SQLite 原子检查点的参考环境启动访客和 Dashboard。进入「合成数据发布」的「DWS 图发布」，填入场景所绑定的基线版本、一份合成知识文档和一条合成数据库目录记录。保存草稿并审核。队列完成后会显示 DWS 变更版本。界面支持一条关系，管理接口 `POST /api/graph-publications` 支持最多 8 个节点和 12 条边。每个文档最多指向两条目录记录，每个节点至少参与一条关系。

公开发布后，新建的研究场景可在 `/decoy/{run_id}/world-graph` 读取该版本的内容。选择会话分支后，只有持有相应分支绑定票据的会话可见。管理端 `POST /api/evidence/runs/{run_id}/bindings?publication=<发布编号>&target=web` 使用已准备好的场景签发票据。基线必须匹配。把 `target` 换为 `ssh`、`mysql` 或 `llm` 可以分别签发其他入口的独立票据。

Web 和模拟 AI 通过请求头 `X-Honeynet-Binding` 使用票据。Cowrie 通过 SSH 客户端的 `HONEYNET_BINDING` 环境请求选择投影。MySQL 使用返回的 `mysql_user`。模拟 AI、Cowrie 和 MySQL 各自继续验证原模拟账户。SSH 文件路径为 `/kb/graph-<文档编号>.md`，MySQL 列表查询展示 `graph_<数据库目录编号>`，模拟 AI 的知识库列表展示同一份文档路径。数据库记录由现有合成目录提供。

撤回公共发布后，后续快照隐藏该图。撤回会话分支后停止签发新票据。已签发票据的版本视图保留至一小时的原有效期。入口独立生成的 AI 蜜饵目前仍有各自投放路径。

## 参考环境的流证据和入口预算

直连协议进程可启用 `HONEYNET_FLOW_DIRECT=1`，与导入器共用至少 32 字节的 `HONEYNET_FLOW_HMAC_KEY`，并按实际观测点设置 `HONEYNET_FLOW_INTERFACE_WEB`、`HONEYNET_FLOW_INTERFACE_LLM` 和 `HONEYNET_FLOW_INTERFACE_MYSQL`。Cowrie Bridge 继续使用 `HONEYNET_FLOW_INTERFACE`。通过 `deploy/run-v2-local.sh flow-import <文件>` 导入默认格式流日志。Dashboard 的攻击链路把已有 Canary 回执和命中与唯一匹配的网络记录一起展示。经过反向代理或 NAT 时，需要采集同一网络观测点的五元组。参考配置当前仅对直接 socket 观测启用这些匹配。

各预算变量默认是 `0`（关闭）。在独立访客进程设置 `HONEYNET_WEB_REQUESTS_PER_10S` 和 `HONEYNET_LLM_REQUESTS_PER_10S` 可按连接来源限制 HTTP 请求。MySQL 使用 `HONEYNET_MYSQL_CONNECTIONS_PER_10S` 和 `HONEYNET_MYSQL_QUERIES_PER_10S` 控制新连接及单连接查询。Cowrie 使用 `HONEYNET_SSH_COMMANDS_PER_10S` 控制单条 SSH 连接的命令预算。在共享代理之前使用来源预算，需要在实际网关分辨访客。Dashboard 告警展示限速和连接终止事件，Cowrie 告警由 Bridge 导入。

## 实体图驱动的自动发布

参考部署使用 `HONEYNET_DWS_ATOMIC=1` 时，启用 `RESEARCH_UNIFIED_PUBLICATION=1`。控制进程把基线世界中的主机、应用、数据库、知识文档和已有关系裁成一份小图，再和本次任务一起提供给模型。AI 使用既有实体 ID 建立新页面与实体之间的关系，整张场景至少引用一组已有关系。单页可以写合成内容，也可以省略来源引用。

先通过模型复核和本地验证，再由 DWS 单写入器提交图。基线版本与发布版本分别保留。新版内容在提交成功后由场景和协议适配器投放。Web 会话内的在线更新成功后只影响该会话。发布失败时，当前场景继续使用上一个已提交版本。Dashboard 的「场景发布」可展开查看首次发布的实体与关系。

内置模拟 HTTP 接口可自动创建。需要本地运行独立实例时，通过 `RESEARCH_AUTO_SERVICE_CAPABILITIES` 指定预先允许的能力，使用逗号分隔。支持 `mysql_catalog`、`isolated_http` 和 `isolated_mysql`。独立实例需要相应的 `HONEYNET_L3_HTTP_IMAGE`、`HONEYNET_L3_MYSQL_IMAGE` 和私有 `HONEYNET_L3_STATE_DIR`。镜像固定摘要，实例无外网连接且一次最多一个。通过就绪检查后才更新当前场景；运行器通过私有 Unix socket 提供版本绑定的合成数据。

## 逐跳 Canary 验证

参考部署在同一配置下启用 `HONEYNET_CANARY_HANDOFFS=1`。按顺序启动访客、Cowrie、Cowrie Bridge 及模拟 MySQL。访客收到并回用 Web Canary 后，响应可返回 SSH 核验凭据。Cowrie 在实际密码核验后通过 SSH 握手 banner 返回 MySQL 核验凭据，Bridge 记录交付并确认它的父级证据。MySQL 核验连接通过 `SELECT next_hop FROM collab_core.handoff` 获取模拟 AI 核验凭据。模拟 AI 的响应给出最终 Web 核验链接。

根 Web 核验凭据有效期为 24 小时。后续凭据约束到指定协议，最多 10 分钟，且到期时间不得晚于父凭据。链路最多衍生四跳。各入口将发送和使用都记录在账本中，Dashboard 显示父级凭据和命中入口。撤销根凭据会使已发放的后代凭据失效。管理面和真实模型继续使用独立的访问控制。

在「场景发布」选择场景，可保存逐跳路径，例如 `web → llm → web`。填写 `web` 时在首个 Web 核验结束。允许的相邻入口为 Web→SSH/MySQL/模拟 AI、SSH→MySQL、MySQL→模拟 AI、模拟 AI→Web。其他组合会由管理接口拒绝。配置从下一次签发根凭据开始生效，已签发凭据按原路径使用。

## 采集限额、发布核验与态势研判

每个研究场景每小时最多采集 1000 条 Web 研究事件。达到限额后，已发布页面与模拟接口照常响应，暂停新增需要研究回执的 Web Canary 和自动适配。已经交付的 Canary 可以继续核验。Dashboard 的「会话证据」显示本小时采集数与因限额未采集的累计数。独立入口限速配置仍按各自的规则执行。

每小时单个 Web 会话最多发起 20 次适配决策，同一场景合计最多 120 次。限额按系统时钟的整点重置。一次决策可能包括单独的模型审核或有限修复，实际供应商调用另受真实模型总预算限制。

参考启动器在原子 DWS 部署中设置访客探测端口。候选版本切换前，控制面以短期凭据向本机访客监听器请求入口页和声明的模拟接口，核对投影图、版本和路由。验证失败时，Dashboard 保留原因，旧发布继续接待。文档、图和自动生成任务分别等待重试，失败任务在最多五次尝试后进入失败状态。

要启用真实模型的全局态势研判，将 `HONEYNET_SITUATION_REPORTS=1` 和 `HONEYNET_SITUATION_MODEL=1` 设在控制进程，并使用原控制面模型配置。每小时快照和可信跨协议 Canary 链会触发报告。每小时最多分析四份，优先分析跨入口报告。供应商接收协议、事件类型和聚合计数。进入 Dashboard 的「态势报告」，查看原始计数、模型研判和带证据的继续观察、通知管理员或提交阻断复核建议。建议在管理页面展示，实际处置需要独立授权。模型不可用时继续显示原始快照。

## 会话分支归档与隔离恢复

协议蜜饵沿用 10 分钟生命周期。Web 在线生成分支默认保留 1 小时，继续浏览时续期。到期后读取基线场景。写入器在所有在线版本和票据都释放后，将相同生成分支的完整历史移入私有 SQLite 的 `archived_graphs`，回收在线实体预算。运营账号可按 DWS 版本访问 `/api/evidence/generated-graphs/{version}`，查看是否归档以及对应图摘要。

主机归属、数据库引擎、服务与世界版本等关键字段用 `fact_claims` 声明。代码将声明与基线快照逐项核对，再替换正文或模拟接口中的 `[[dws:声明编号]]`。普通合成正文继续经过模型语义复核。

自动 Web 场景最多 8 个页面、8 个模拟 HTTP 接口和 32 条图边。每个节点最多连接 8 条边，入边与出边合并计数。校验覆盖页面链接、接口关系、DWS 锚点和选入的已有关系。SSH、MySQL 和模拟 AI 的单个蜜饵最多选用两个直接相关的 DWS 实体。管理端提交的文档与目录图最多 8 节点、12 边，每个文档最多关联两个目录，每个目录最多接受四篇文档的关联。

备份前停止参考配置中的访客写入器、控制工作进程和传感器桥接。命令在本机 SQLite 参考配置上演练，备份目录含私有研究账本，使用部署端的私有存储。

```sh
.venv/bin/python -m deploy.state_backup snapshot --state /path/to/state --cowrie /path/to/ssh/world.sqlite --destination /path/to/new-backup
.venv/bin/python -m deploy.state_backup restore --bundle /path/to/new-backup --destination /path/to/new-restore
```

恢复目录必须尚未存在。将恢复目录中的 `state` 和 `cowrie/world.sqlite` 分别配置为状态目录和终端投影。恢复前会核对文件校验值和两个快照的 DWS 版本。

使用同一个版本完成固定、规则和真实模型对照，可运行 `.venv/bin/python -m research.evaluate_modes`。仅使用合成 Web 请求和独立临时数据库，实际模型调用需要已有控制面模型配置。输出为逐任务匹配、负样本、延迟、模型调用次数和供应商报告的 token 用量。
