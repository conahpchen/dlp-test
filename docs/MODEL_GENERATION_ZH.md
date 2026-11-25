# 模型生成配置与实测

在「场景模板」提交后，Dashboard 显示排队、准备 DWS 资料、等待草稿、校验和模型复核，以及当前阶段的任务耗时。启用 SSE 后，控制面在收到推理或正文事件时更新阶段。模型内部推理文本不会写入任务状态，也不会随响应返回访客。模型首个事件的时间仍取决于网关何时开始转发。

保存的模板发送业务目标、按需字段说明、可引用的事实占位符和少量摘录。普通模板沿用一条相关 DWS 关系及最多两段摘录。模板明确提到多个入口或已有资产时，程序从同一份 DWS 快照找出能连接目标的关系，最多选取六个实体和六条关系。跨入口模板最多提供四段命中段落，每段至多 360 字符。明确引用的 `/kb/...` 路径需要在当前快照中有可用正文。

例如模板要求 SSH 日志和模拟 MySQL 目录使用同一个库名。程序在生成前确认当前 Cowrie 对应主机、MySQL 托管关系及目录对外显示的名称。同一模板中的资产名由程序按事实占位符填充。缺少入口、关系或点名文档时，Dashboard 显示相应原因，该任务停在准备 DWS 资料阶段。

模板任务最多调用模型两次。程序要求正文或模拟接口响应覆盖指定资产，并用完整授权的 DWS 投影和原文校验。草稿通过后，独立模型复核正文、模拟接口返回内容、相关事实和必要摘录。草稿和复核的提示内容分别有 16 KiB 和 24 KiB 上限。超过上限时保留明确的失败状态。自动场景使用独立的生成链路。

| 控制面环境变量 | 用途 |
| --- | --- |
| `RESEARCH_MODEL_THINKING` | 默认 `auto`，直连 DeepSeek 官方接口时关闭思考，其他网关使用服务端默认。支持 `disabled`、`enabled` 和 `omit` |
| `RESEARCH_MODEL_THINKING_FIELD` | 默认 `thinking`，发送 `{"type":"disabled"}` 等对象。支持按网关文档改为 `enable_thinking` 或 `chat_template_kwargs` |
| `RESEARCH_MODEL_STREAM` | 默认 `0`。支持 OpenAI SSE 的网关可设 `1`。如果网关支持 SSE 但拒绝 `stream_options`，可设 `RESEARCH_MODEL_STREAM_USAGE=0` |
| `RESEARCH_MODEL_TIMEOUT_SECONDS` | 单次读取等待，默认 15 秒，范围 1 至 600 秒 |
| `RESEARCH_MODEL_TOTAL_TIMEOUT_SECONDS` | 单次模型调用的总时限，范围 1 至 900 秒。默认比读取时限多 30 秒。达到时限后控制面停止等待 |
| `RESEARCH_MODEL_DRAFT_MAX_TOKENS` / `RESEARCH_MODEL_REVIEW_MAX_TOKENS` | 草稿和复核的输出上限，默认分别为 3500 和 800。网关可能把推理计入输出额度 |
| `RESEARCH_MODEL_DAILY_TOKENS` | 控制面每日 Token 上限，默认 100 万，UTC 零点切换。调用前预留额度，拿到完整用量后按实际输入和输出结算。缺少可信用量时保留预留额度 |

部署本版本时同步更新并重启 control 与 Dashboard。后续修改模型环境变量，只需重启 control。内网 GLM 如果确认支持 `thinking` 和 SSE，可以先用以下配置试跑同一个模板。

```sh
export RESEARCH_MODEL_THINKING=disabled
export RESEARCH_MODEL_STREAM=1
export RESEARCH_MODEL_TIMEOUT_SECONDS=90
export RESEARCH_MODEL_TOTAL_TIMEOUT_SECONDS=120
```

根据 Dashboard 的具体状态排查。连接超时检查到网关的网络。首个响应超时检查网关等待时间、思考模式和输入大小。流中断检查上游能否持续转发 SSE 以及空闲时限。达到总时限检查单次调用耗时。预算、并发、熔断和供应商限流有各自的提示。预算剩余不足时，先看「部署与运行」的「今日已计入 Token」；供应商报错导致短时熔断时，连续失败后默认冷却 30 秒。

本地使用真实 DeepSeek `deepseek-flash`，关闭思考并启用 SSE，用同一个合成数据库目录核验模板实测。与上一版相比，首轮草稿输入从 2,209 降到 432 Token，两次复测的复核输入分别为 608 和 714 Token，上一版是 1,858 Token。两次均只用一次草稿和一次复核，分别在 4.83 秒和 6.53 秒得到候选。随后通过 DWS 发布其中一份候选，访客页面和世界图均返回 HTTP 200。同环境另测 SSH 与模拟 MySQL 目录核验模板。查询预检选出三个实体、两条关系，草稿输入 641 Token、复核输入 740 Token，5.04 秒得到候选。候选发布后，访客页面与模拟 MySQL 目录查询核对到同一库名，Dashboard 的发布图也显示了两条 DWS 关系。这条验证覆盖候选生成、DWS 发布和既有目录的一致性。内网 GLM 的时延仍需在实际网关复测。
