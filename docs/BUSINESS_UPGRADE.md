# 研发服务平台业务增强版

本次以设计方案 §02–04 的“同一个虚拟企业、DWS 单一事实源、业务关联”及 §07 的第一类业务客户端为基础，完善 ZIP 中已有业务平台。交互参考 Jira 的问题跟踪、禅道的研发流转及 ServiceNow 的服务管理习惯，使用独立的澄川品牌，不复制商业产品素材。

## 启动

```bash
cd /path/to/ai-honeypot
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn business.app:app --host 127.0.0.1 --port 8080 --workers 1
```

浏览器访问本机 8080 端口。Windows 可使用 `.venv\Scripts\python.exe`。业务界面包含工作台、工单中心、应用目录、资产与依赖、变更发布、数据库中心、知识库、公告及审计。

合成员工账号：dev01（开发）、test01（测试）、ops01（运维）、dba01（数据库）、biz01（运营）、manager01（负责人）；初始口令为账号加 `-password`，用于隔离的蜜网合成身份。人员姓名和部门同时投影自 DWS，并与 Agent 配置保持一致。

## 做了什么

- 删除独立的前端种子工单缓存。工单列表、详情、评论、状态、分派与审批均读写同一 DWS 对象；`/api/tickets/all` 与 `/api/tickets` 一致，修复静态路由被动态详情路由遮挡的问题。
- 统一编号。新建返回的 key/ticket_id 可直接查询，不再出现创建和存储编号不同的情况。
- 评论使用独立接口，不再误调用审批。状态机拒绝跳转；管理审批限制 manager01。传入 version 时拒绝过期修改，前端写操作携带读到的版本。
- 初始 44 条工单：8 条具体业务事项，以及 12 周 × 3 个应用的周期巡检、备份抽检和接口复核历史。所有时间相对世界种子固定生成，早于世界当前时间，处理时间线单调；没有依赖墙钟的随机填充。
- 3 个应用、1 个数据库视图（含数据字典、慢查询摘要、备份策略）、3 篇知识文档和公告。APP-COLLAB → APP-DATA、INC-2401 → CHG-0821 → DB-CORE 慢查询记录互相关联。主机地址和服务端口直接来自原始 DWS host/relation，不另造监控健康值。
- 变更 `draft → tested → approved → deployed → rolled_back`。必须有工单、执行计划和回滚方案；test01 提交通过报告，manager01 审批，ops01 发布或回滚。数据库与部署内容是业务记录，不会执行实际 SQL 或系统命令。
- 6 种 Agent 通过相同业务 API 完成 7 步工作流：运营创建、开发受理并提变更、测试、审批、运维发布、DBA 检查、负责人关闭。任何接口失败立即中止报告，不再忽略失败后宣称成功。
- 新类型有 YAML 描述及 JSON Schema；业务写入口执行相应 Schema 校验。DWS 内核和回放语义保持不变，未宣称实现全局通用 YAML 约束执行引擎。
- 业务审计行不出现 Agent 分类标记；分类留在统一采集事件。`/api/events` 和 `/api/events/ingest` 是控制面，必须配置 HONEYNET_CONTROL_TOKEN 并携带 x-control-token；未配置时返回 404。员工登录不授予采集控制面权限。关闭公开 OpenAPI 文档，并移除集成业务网关响应中的 x-deception 分析元数据。

## 数据保留与边界

默认业务写入及统一事件更新会原子保存至 `var/business-state.json`，保留 DWS 全部事实版本、逻辑时钟、业务审计和采集事件。重启后恢复；登录会话不保留，需要重新登录。文件以 0600 权限写入，请保留该目录的持久卷。

- `BUSINESS_STATE_FILE=/absolute/path/state.json`：指定本地检查点。
- `BUSINESS_STATE_FILE=''`：关闭本地检查点。
- 显式指定 GATEWAY_WORLD_SEED 或 HONEYNET_PG_DSN 时，默认关闭本地检查点，避免把外部种子和旧状态混合；可用 BUSINESS_STATE_FILE 显式开启。
- 检查点损坏会报错，不会静默重建世界。新种子不自动覆盖已保存的世界；重置前应先备份状态文件。
- 该实现是单 worker 的本地运行方式。未实现多进程共享写入、数据库事务持久层及分布式恢复。接口 version 为兼容旧 Agent 客户端保留可选，旧客户端未携带版本时使用进程内串行写入。
- 纯登录或只读访问新增的审计行，在下次业务写入或事件更新时一并保存；没有承诺每次读取均即时落盘。

应用目录、资产、数据库视图和知识文档目前是可浏览的 DWS 投影，未提供完整 CMDB 编辑器、实际数据库管理、代码托管、文件上传或外部链接执行功能。未配置 OpenSandbox、11 节点部署、24 小时连续运行、动态诱捕策略或真实 MySQL 服务；这些不是本次代码测试的已验收项。原独立 entries.gateway 仍保留自身研究接口，部署时应区分控制面和暴露面。

## 验证

```bash
python -m pytest tests -q
python -m replay.harness verify --world tests/fixtures/world_seed.demo.json --events tests/fixtures/events.demo.jsonl
```

新增测试覆盖投影一致性、时间线、评论不改变状态、过期版本冲突、并发修改、引用、岗位分权、发布/回滚全流程、控制面隔离及全版本状态恢复。既有 Agent 工作流测试现覆盖 7 步，所有非 200 响应会使流程失败。

浏览器验证覆盖登录、8 个业务页、新建工单、评论及脚本错误检查；截图见交付目录。单元测试数量与最终验证结果见交付说明。


浏览器脚本：`tests/browser_smoke.cjs`，需可用的 Node.js、playwright 与 Chromium。可通过 BASE_URL、CHROMIUM_PATH、BROWSER_OUTPUT 指定地址、浏览器和截图目录。脚本会创建一条工单并追加评论，应对可重置的验证实例运行。

本次运行结果：305 项测试通过；两次内核回放逐字节一致；真实 HTTP 七步工作流闭环（6 角色，11 条 Agent 事件）；浏览器 8 模块导航、评论和创建通过，无页面脚本错误。未执行 Docker / K8s / OpenSandbox 部署验收。
