# 动态蜜网 v2.1 架构

DWS 持有合成主机、身份、数据、服务及业务对象事实。启动种子引入初始事实，发布应通过 DWS CAS 提交。单写入者在本地原子替换 JSON checkpoint。读取方可读取其中一个完整版本。Research SQLite 保存投放计划、观测和审核记录，现有 HTTP 场景正文仍保存在 Research 投影中。

## 协议与会话契约

- SSH 回放从连接固定版本，登录按固定版本核验，命令只读取固定版本与自身写入；模拟写入版本单独标记并随 checkpoint 恢复，其内容对其他会话不可见。现有 Cowrie 桥为采集器，终端投影仍需接入。
- 假 AI API 的已认证会话由凭据和客户端标签共同限定。模型清单与聊天复用 DWS 视图。匿名模型清单展示当前服务。
- Web 自动入口在访客首次命中激活场景时固定 run。其文档来自 run 创建时的 DWS 版本，现存访客保持原绑定。
- MySQL 目录的 TCP 客户端连接从 checkpoint 建立固定投影，支持认证、`SELECT 1` 和 `SELECT name, engine FROM collab_core.catalog`。使用独立合成账号，无真实数据库后端。

## 事件与模型契约

统一事件沿用 `contracts/event.schema.v1.json`。证据记录保留 `event_id`、`entry`、`session`、`what.kind` 与证据引用。回放响应及事件的 `dws_version` 表示会话绑定版本。原始或脱敏证据内容留在采集侧。模型事件只包含内部事件 ID、闭集活动分类和合法场景引用。未知输入保留为 `unknown`。

Proposal 的后续通用契约应包含基线版本、声明式操作、引用、能力需求与资源预算。状态依序为 `proposed → validated → approved → prepared → healthy → active`。任何准备失败应保留先前活跃版本并记录失败。回滚需校验当前发布标识并记下前驱。当前 HTTP 研究场景已有结构校验和 SQLite 激活/回滚，尚未接入上述 DWS 通用状态机。

L0 是已有文件/业务投影，L1 是复用 HTTP 入口，L2 是明确启动轻量协议，L3 需要隔离及人工批准。当前 MySQL 目录是手动启动的 L2 子集。

管理端模板采用追加修订。控制面按显式模板修订号创建 run，保存模板不会激活场景。
