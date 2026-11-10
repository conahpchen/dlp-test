# 澄川研发服务平台 · 蜜网业务增强版

新增业务模块、DWS 关联数据、七步 Agent 发布工作流与本地状态恢复。启动及边界见 [业务增强说明](docs/BUSINESS_UPGRADE.md)。

下方为 ZIP 原有内核文档，历史验收数字不代表本次运行结果。

---

# ai-honeypot · AI 密网 M0 可回放内核

单一事实源（DWS）驱动的多入口欺骗系统——「一个攻击者，一个世界」。
本仓库当前实现 **M0 里程碑（可回放内核）**，接口与字段以 [`../12-开发契约.md`](../12-开发契约.md) 为准，
排期与验收以 [`../11-执行架构.html`](../11-执行架构.html) 为准，开发指导见 [`../13-整体实施方案.md`](../13-整体实施方案.md)。

## G0 验收状态（本机已验证，2026-10-23）

| 判据（11 §04） | 结果 | 工具 |
|----------------|------|------|
| 一条 SSH 事件落 DWS | ✅ 本机走通「Cowrie 日志 → 桥接 → 统一事件 → 世界」全链路；Postgres 落库待内网 Linux（本机无 Docker） | `entries/cowrie/bridge.py` + `replay.harness --pg-dsn` |
| 回放逐字节一致 | ✅ `VERIFY PASS`（2 次回放 SHA-256 相同） | `python -m replay.harness verify` |
| 10⁴ 随机轨迹不变量 0 违反 | ✅ **10,000 轨迹 / 164,003 事件 / violations=0 / determinism_failures=0（22.8s）** | `python -m replay.acceptance` |

单元/契约测试：`242 passed`。

## 目录

```
contracts/   契约的机器可执行形态：事件 JSON Schema、CSVR 规则
dws/         DWS 语义核心（物理定律，语义冻结）：hlc/facts/world/invariants + SQL + Postgres 装载
replay/      回放 harness：canonical/engine/harness CLI/trajectories/acceptance
generation/  grounded 模板生成 + 泄漏守卫（M0 无 LLM；措辞层 M1 接入）
identity/    身份解析最小版（IP+凭据指纹；多视图 M1）
scorer/      CSVR v0（T/C/M/I 七条规则 + recall 区间申报纪律）
audit/       SLO 聚合（12 §7）
entries/     Cowrie SSH 入口 + 统一事件桥接（合规：密码即时哈希、IP /24 截断）
deploy/      两阶段编排（内网 PoC 默认绑 127.0.0.1；公网覆盖层 + 加固 checklist）
judgment/    判定与策略（M1 起填充，现为空属预期）
experiments/ 实验框架（M2–M3 起填充）
tests/       契约测试（schema/不变量/确定性/grounding/scorer/桥接/SLO）
```

## 快速开始（开发机）

```bash
pip install -r requirements.txt

# 全量测试（含 200 条 fuzz 轨迹；HONEYNET_FUZZ_N=2000 可加大）
python -m pytest tests -q

# G0 三判据本机复验
python -m replay.harness verify --world tests/fixtures/world_seed.demo.json \
       --events tests/fixtures/events.demo.jsonl
python -m replay.acceptance                       # 默认 10^4 轨迹
python -m replay.acceptance --trajectories 200    # 快速冒烟

# 产出回放工件（responses.jsonl / world_diff.json / run_manifest.json / metrics.json）
python -m replay.harness run --world tests/fixtures/world_seed.demo.json \
       --events tests/fixtures/events.demo.jsonl --out artifacts/demo-run

# Cowrie 桥接联调（无 Docker）
python -m entries.cowrie.bridge --in tests/fixtures/cowrie.sample.jsonl \
       --out artifacts/bridge-events.jsonl --node sensor-a --dst-host web01
```

## 内网 Linux 部署（阶段一，13 §3.2）

```bash
cp deploy/.env.example deploy/.env       # 设置 POSTGRES_PASSWORD
cd deploy && docker compose up -d        # postgres + redis + cowrie + bridge
# DDL（dws/sql/001_init.sql）自动执行；桥接以 --follow 持续转换 Cowrie 日志

# 事件落 DWS + 世界回填
export HONEYNET_PG_DSN="postgresql://honeynet:***@127.0.0.1:5432/honeynet"
python -m replay.harness run --world ... --events ... --out artifacts/run --pg-dsn "$HONEYNET_PG_DSN"
```

公网迁移（阶段二）见 `13-整体实施方案.md` §3.3 checklist 与 `deploy/compose.prod.yml`。

## 开发纪律（13 §05，违反即 PR 拒绝）

1. **契约先行**：字段/断言/规则改动先改 `../12-开发契约.md`（走其 §10 变更通道）
2. **先断言后代码**：I1–I4 测试先于实现
3. **回放一致进 CI**：每个 PR 跑两遍 harness 比对哈希
4. **确定性**：处理路径禁读系统时钟、禁未播种随机
5. **DWS 与回放语义冻结**（K6）：改语义 = 触发 G0 返工
