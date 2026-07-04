# 情报收集员 Agent 代码包 V0.6

这是 Agent交易公司多 Agent A 股交易辅助系统中的 **情报收集员 Agent**。它面向 OpenClaw 多 Agent 运行环境设计，负责 Demand → Ticket → Message → 工具调用 → 质量闸门 → 事件/特征/日报的采集闭环。

本 Agent **不输出买卖建议、仓位、目标价或交易指令**。它只做采集、结构化、质量检查、消息投递、Checkpoint、Memory 和日报。

## 0. V0.6 关键变更（按设计说明书补齐功能）

1. **消息化查询服务（设计 §11）**：新增 `query.intelligence.request` / `query.intelligence.response` 消息和 `INTELLIGENCE_QUERY_REQUEST/RESPONSE_TICKET`。其他 Agent 通过队列发起查询（`recent_events`、`market_features`、`collection_status`、`data_quality`、`tool_capabilities`），本 Agent 消费后经内部 Reader 执行并把结果 Ticket + 消息投回请求方。CLI：`query request` / `query responses`。
2. **Demand 消息流（设计 §6.2）**：`demand register` 落库后发布 `demand.registered`，`suspend/resume/cancel` 发布 `demand.changed`。新增 `RuntimeController`（`runtime.py`）：`runtime tick` 先消费 demand 消息，Demand 被暂停/取消时**取消其未完成的 Request/Task Ticket 并 ack 对应消息**，再编译活跃 Demand。
3. **动态池目标解析（设计 §7.1）**：新增 `pool_members` 表和 `PoolRepository`；`target_scope.scope_type: dynamic_pool` 会按 `pool_layers` + `filters`（sellability / exclude_st / exclude_suspended）解析成具体标的。CLI：`pool set/remove/list`。
4. **行情特征增强（设计 §7.4）**：新增 prev_close/涨跌停价与距离（按板块 10%/20%/30%、ST 5%）、`hit_limit_up/down`、日内区间位置、20 日同时间段成交额量比（经本地 `query bars` 富化，可配置开关）；异动改为**多条件阈值**（收益率、量比、涨跌停距离、异常分），触发跌停/大幅下跌时按 `risk_review` 规则升级为 urgent 并路由 `risk_control`。
5. **能力验证扩展（设计 §9.4）**：除 5m/15m 频率外，新增 `cli_available`、`eastmoney_cookie`（默认关）、`trading_status`、`historical_bars_1d`、`query_meta_summary` 检查，并记录 `recommended_intraday_mode`。支持 `run_on_startup`（每交易日一次）与 `run_pre_market`（tick 在盘前自动调度能力验证任务）。CLI：`runtime capability-validate`。
6. **交易日历（设计 §7.3）**：`market_calendar.holidays` / `extra_trading_days` 配置节假日与调休；`market_phase` 据此判定 `non_trading_day`。
7. **下游消息补齐（设计 §5.2）**：任务完成发布 `collection.result`；MIC 覆盖缺口发布 `coverage_gap.created`；日报发布 `report.collection_daily`；`checkpoint.created` 由 `queue.publish_checkpoint_messages` 控制（默认关）。
8. **日报补齐（设计 §15.2）**：新增 Demand 覆盖情况、Message 处理统计、成本与调用次数（含 MIC 预算使用汇总）、次日补采建议四个章节；`report daily --format json|html|both`。
9. **消息 TTL**：`messages.expires_at` + `expired` 状态（含旧库自动迁移），过期消息不可租约。
10. **cadence_profile 命名档案**：Demand 可引用 `cadence_profiles.<name>`（`market_snapshot.bucket_size`、`mic_black_swan_scan.interval/enabled`）覆盖池层默认节奏。
11. **运维补齐**：`queue publish`、`config validate`、`db backup`（SQLite backup API 归档）、`init-db --reset`、`agent checkpoint`（手动 checkpoint）、session 超时自动轮转（`runtime.session_rotate_minutes`）。

未改动的等价实现：`message_attempts`/`dead_letters`/`report_artifacts` 仍以 messages 字段、`status='dead'`、`daily_collection_reports` 列承载；实时行情 Python 层能力记录为 `unknown`（首版 CLI 未暴露该子命令）。

本版验证：`PYTHONPATH=src pytest -q` 33 个离线测试全部通过；CLI 冒烟覆盖 `config validate`、`pool`、`query request → agent run-once → query responses` 全链路、`demand register → runtime tick` 消费 `demand.registered` 并按动态池编译、节假日 tick 判定 `non_trading_day`、盘前 tick 自动调度能力验证任务、`queue publish`、`agent checkpoint`、`db backup`、`report daily --format json` 新章节输出。

## 0.1 V0.5.1 关键变更

本版根据代码审核结果修复了以下问题：

1. `mic.db` 如需随包保留，已移动到 `data/mic.db`，不再放在项目根目录。
2. OpenClaw 模型占位符被硬拦截：`REPLACE_WITH_REGISTERED_OPENCLAW_MODEL`、`default`、`openclaw/default` 均不能在 `allow_openclaw_default: false` 时启动。
3. `workspace_root` 支持配置、环境变量和 OpenClaw CLI 自动发现；工具路径不再依赖进程 CWD。
4. SQLite 拆成三个逻辑 store：
   - `state_sqlite_path`：本 Agent 私有状态，保存 memory、checkpoint、session、heartbeat、circuit breaker、tool capabilities。
   - `bus_sqlite_path`：共享消息队列和 Ticket Bus。
   - `data_sqlite_path`：Demand、采集任务、工具运行、结构化事件、行情特征、质量问题、日报。
5. 工具返回 retryable failure 时不再直接 ack；会根据错误类型 `nack` 重试或进入 dead-letter。
6. 非交易日、午休、非盘中阶段不再生成盘中行情快照；black swan 扫描是否允许在午休/非交易日/盘后运行由 YAML 控制。
7. stock_data 盘中能力验证不再只看 `status=success`，必须通过 `query bars` 确认存在 5m/15m 行情行。
8. `QualityGate` 现在执行 `quality.minimum_quality_for_public_pool` 阈值。
9. `tools.*.enabled` 已在执行层生效。
10. 盘中 fetch 后会再走 `query bars`，使用已落库行情生成特征，并显式记录 5m 聚合或 15m 降级。
11. Demand version 元数据会进入 `COLLECTION_REQUEST_TICKET`。
12. Demand 重复注册时，payload 内的 `demand_id` 会与主键保持一致。

本版在 V0.5 基础上合入（V0.5 已包含：日志输出到 `logs/`、MessageQueue 抽象与 nack/extend_lease/dead-letter、heartbeat、circuit breaker、崩溃恢复、cadence 节流、Demand 生命周期 CLI、`agent status`、日报扩充等）。

本机集成验证结果：

- `python -m compileall -q src` 通过。
- `PYTHONPATH=src pytest -q`：19 个测试全部通过。
- CLI 冒烟：`init-db` 正确生成三个 SQLite store；非交易日 `runtime tick` 不再产出盘中任务；真实工具调用失败时消息被 `nack` 回到 `open` 状态并携带结构化 retryable 错误（不再被错误 ack）；`agent status`、`runtime recover`、`report daily` 均正常。
- 模型占位符硬拦截已验证：未替换 `openclaw.model.primary` 时 `load_config` 直接抛出 `ConfigError`，Agent 无法启动。

## 1. 运行边界

可以做：

- 注册和管理 Demand。
- Runtime tick 将 Demand 编译成 `COLLECTION_REQUEST_TICKET`。
- 消费消息并生成 `COLLECTION_TASK_TICKET`。
- 调用真实 `market_intelligence_collector`。
- 调用真实 `stock_data_collector`。
- 生成结构化事件、10 分钟行情特征、数据质量 Ticket、故障 Ticket 和日报。
- 保存本 Agent 独立 memory、checkpoint、session 和 heartbeat。

不能做：

- 不下单。
- 不绕过风控。
- 不生成买入/卖出/仓位/目标价。
- 不绕过验证码、反爬或付费墙。
- 不伪造 Cookie。
- 不把 Token、Cookie、API Key 写入日志、Memory 或 README。

## 2. 安装

```bash
cd intelligence_collector_agent
python -m pip install -e .
```

开发测试：

```bash
python -m pip install -e ".[dev]"
PYTHONPATH=src pytest -q
```

当前离线内部测试不调用真实 MIC 或 stock_data；真实工具测试通过 CLI 在你的本地工具环境中执行。

## 3. 配置 OpenClaw 模型

编辑：

```text
config/intelligence_collector.yaml
```

必须把占位符替换为 OpenClaw 已注册模型：

```yaml
openclaw:
  model:
    primary: "openai/gpt-5.5"
    fallbacks:
      - "deepseek/deepseek-reasoner"
    require_registered: true
    allow_openclaw_default: false
```

硬规则：

- `allow_openclaw_default: false` 时，`primary` 不能为空。
- `primary` 不能是 `REPLACE_WITH_REGISTERED_OPENCLAW_MODEL`。
- `primary` 不能是 `default` 或 `openclaw/default`。
- 这个 Agent 不会静默继承 OpenClaw 全局 default model。

校验模型：

```bash
intel-agent --config config/intelligence_collector.yaml openclaw validate-model
```

如果本地没有 OpenClaw CLI，校验命令会给出 warning；在 OpenClaw host 中应返回 success。

## 4. workspace_root 与路径规则

`runtime.workspace_root` 支持三种方式：

```yaml
runtime:
  workspace_root: "auto"      # 推荐：读取 OPENCLAW_WORKSPACE_ROOT / OPENCLAW_WORKSPACE
  # workspace_root: "openclaw" # 尝试 openclaw workspace path，然后环境变量 fallback
  # workspace_root: "/abs/path/to/workspace"
```

解析顺序：

1. 如果显式配置绝对路径或相对路径，则按配置解析。
2. `auto` 会读取 `OPENCLAW_WORKSPACE_ROOT`、`OPENCLAW_WORKSPACE`、`OPENCLAW_WORKDIR`。
3. `openclaw` 会尝试调用 OpenClaw CLI 获取 workspace path，再回退环境变量。
4. 如果都没有，配置文件在 `config/` 目录下时，默认使用 `config/..` 作为 workspace root。

所有 runtime 路径均相对 `workspace_root` 解析，不依赖进程 CWD。

工具路径也按 `workspace_root` 解析。第一版默认不写相对路径：

```yaml
tools:
  stock_data_collector:
    enabled: true
    config_dir: null      # 可填绝对路径；null 表示让 stock_data_collector 自行解析配置/env
    working_dir: null     # 若 CLI 不可 import，可填 stock_data_collector 项目根目录绝对路径
```

## 5. SQLite 三库边界

默认配置：

```yaml
runtime:
  state_sqlite_path: "data/intelligence_collector_state.db"
  bus_sqlite_path: "data/ticket_bus.db"
  data_sqlite_path: "data/intelligence_collector_data.db"
```

| Store | 是否私有 | 内容 |
|---|---:|---|
| state DB | 是 | `agent_sessions`、`agent_checkpoints`、`agent_memories`、`runtime_heartbeats`、`circuit_breakers`、`tool_capabilities` |
| bus DB | 共享 | `messages`、`tickets`、`ticket_events` |
| data DB | 可共享 | `collection_demands`、`collection_tasks`、`collection_runs`、`structured_events`、`market_features`、`data_quality_issues`、`coverage_gaps`、`daily_collection_reports` |

第一版仍然使用 SQLite + WAL。三个路径可以在小型本地测试中指向同一个文件，但 OpenClaw 多 Agent 运行时建议至少将 state DB 与 bus/data DB 分开。

初始化：

```bash
intel-agent --config config/intelligence_collector.yaml init-db
```

输出会显示三个 SQLite 路径。

## 6. Demand Registry

注册 Demand：

```bash
intel-agent --config config/intelligence_collector.yaml demand validate \
  --file examples/demands/held_sellable_10m.json

intel-agent --config config/intelligence_collector.yaml demand register \
  --file examples/demands/held_sellable_10m.json \
  --activate
```

管理生命周期：

```bash
intel-agent --config config/intelligence_collector.yaml demand suspend --demand-id demand_xxx
intel-agent --config config/intelligence_collector.yaml demand resume  --demand-id demand_xxx
intel-agent --config config/intelligence_collector.yaml demand cancel  --demand-id demand_xxx
```

Demand 重复注册时按 `idempotency_key` 升版本，`collection_demand_versions` 保留历史版本。`DemandRegistry.get()` 会返回：

```json
{
  "demand_id": "...",
  "current_version": 2,
  "_registry": {
    "current_version": 2,
    "status": "active",
    "created_at": "...",
    "updated_at": "..."
  }
}
```

## 7. Runtime tick：Demand → Ticket → Message

```bash
intel-agent --config config/intelligence_collector.yaml runtime tick \
  --now "2026-06-11T10:30:00+08:00"
```

流程：

1. 读取 active Demand。
2. 根据 `market_phase`、交易日、Demand 有效期和 `schedule_window` 判断是否编译。
3. 写入 `COLLECTION_REQUEST_TICKET` 到 bus DB。
4. 投递 `intelligence.collection` message 到 bus DB。

手工指定 phase：

```bash
intel-agent --config config/intelligence_collector.yaml runtime tick \
  --now "2026-06-11T12:10:00+08:00" \
  --market-phase lunch_break
```

默认 phase 规则：

- 周末视为 `non_trading_day`。
- 09:30–11:30、13:00–15:00 是 `intraday`。
- 11:30–13:00 是 `lunch_break`。
- 其他窗口按 YAML 中 `schedule.market_windows` 判断。

## 8. 盘中频率与调度约束

10 分钟只是默认值，所有频率都在 YAML 中配置：

```yaml
cadence:
  intraday_bucket_minutes: 10
  held_sellable_intraday_minutes: 10
  held_t1_locked_intraday_minutes: 30
  trading_candidate_intraday_minutes: 10
  watchlist_intraday_minutes: 60
  black_swan_held_sellable_minutes: 60
  black_swan_candidate_minutes: 120
```

盘中 phase 约束：

```yaml
schedule:
  allow_non_trading_day_intraday: false
  allow_lunch_break_intraday: false
  allow_off_hours_intraday: false
  allow_non_trading_day_black_swan: false
  allow_lunch_break_black_swan: true
  allow_off_hours_black_swan: true
```

默认行为：

| phase | 盘中行情快照 | black swan 扫描 |
|---|---:|---:|
| intraday | 允许 | 允许 |
| lunch_break | 不允许 | 允许 |
| non_trading_day | 不允许 | 默认不允许，Demand 和 config 都允许时才运行 |
| pre/post/off_hours | 不允许 | 默认允许 |

## 9. 运行 Agent

运行一次：

```bash
intel-agent --config config/intelligence_collector.yaml agent run-once
```

运行到队列空：

```bash
intel-agent --config config/intelligence_collector.yaml agent run-until-idle --max-messages 100
```

查看状态：

```bash
intel-agent --config config/intelligence_collector.yaml agent status
```

状态输出包括：

- state / bus / data DB 路径。
- 最新 session。
- 最新 checkpoint。
- 最新 heartbeat。
- 队列状态分布。
- 未关闭 Ticket 统计。
- 未关闭 collection task 统计。
- 最新工具能力验证。
- circuit breaker 状态。

## 10. 消息队列与 retry 语义

`SQLiteMessageQueue` 状态机：

```text
open → in_progress → done
                    → open   # retryable nack
                    → dead   # non-retryable 或超过 max_attempts
```

工具错误处理：

| 错误 | Message 动作 | Ticket 动作 |
|---|---|---|
| `RATE_LIMITED`、`PROVIDER_TIMEOUT`、`PROVIDER_UNAVAILABLE`、`MIC_TOOL_FAILED`、`STOCK_DATA_TIMEOUT` | `nack` retry | task ticket 保持 open |
| `TOKEN_MISSING`、`AUTH_FAILED`、`PERMISSION_DENIED` | ack | DATA_QUALITY / FAULT，等待人工 |
| `STORAGE_FAILED`、`RAW_SAVE_FAILED` | ack | P0，等待人工 |
| high / critical conflict | ack | DATA_QUALITY，人工复核 |
| `EMPTY_RESULT` | ack | 通常只记录，不作为 retry |
| circuit open | `nack` retry | 冷却后重试 |

运维命令：

```bash
intel-agent --config config/intelligence_collector.yaml queue list --status open --topic intelligence.collection
intel-agent --config config/intelligence_collector.yaml queue dead-letter
intel-agent --config config/intelligence_collector.yaml queue inspect --message-id msg_xxx
intel-agent --config config/intelligence_collector.yaml queue retry --message-id msg_xxx
intel-agent --config config/intelligence_collector.yaml queue publish --topic intelligence.collection --ticket-id ticket_xxx
```

V0.6 新增运维命令：

```bash
# 配置校验（打印解析后的路径与工具开关）
intel-agent --config config/intelligence_collector.yaml config validate

# 股票池维护（dynamic_pool Demand 的目标来源）
intel-agent --config config/intelligence_collector.yaml pool set --layer current_holding --ticker 300750.SZ --sellability sellable --company-name 宁德时代
intel-agent --config config/intelligence_collector.yaml pool list --layer current_holding
intel-agent --config config/intelligence_collector.yaml pool remove --layer current_holding --ticker 300750.SZ

# 消息化查询（模拟其他 Agent 发起查询；由 agent run-once / run-until-idle 应答）
intel-agent --config config/intelligence_collector.yaml query request --query-type recent_events --ticker 300750.SZ --source-agent analysis_agent_x
intel-agent --config config/intelligence_collector.yaml query responses

# 手动 checkpoint / SQLite 归档备份 / 重置数据库
intel-agent --config config/intelligence_collector.yaml agent checkpoint
intel-agent --config config/intelligence_collector.yaml db backup
intel-agent --config config/intelligence_collector.yaml init-db --reset

# 盘前能力验证（直接执行，或让 tick 调度）
intel-agent --config config/intelligence_collector.yaml runtime capability-validate
intel-agent --config config/intelligence_collector.yaml runtime tick --now "2026-06-11T08:30:00+08:00" --run-capability-validation
```

过期 lease 恢复：如果 `attempts < max_attempts`，回到 open；否则进入 dead-letter。

## 11. 长期运行、Checkpoint、Memory、恢复

本 Agent 有自己的私有 state DB：

- `agent_sessions`
- `agent_checkpoints`
- `agent_memories`
- `runtime_heartbeats`
- `circuit_breakers`
- `tool_capabilities`

恢复命令：

```bash
intel-agent --config config/intelligence_collector.yaml runtime recover
intel-agent --config config/intelligence_collector.yaml agent resume --max-messages 100
```

恢复流程：

1. requeue 过期 lease。
2. 已完成 Ticket 的消息补 ack。
3. dead-letter message 生成 `FAULT_TICKET`。
4. 孤儿 `COLLECTION_REQUEST_TICKET` / `COLLECTION_TASK_TICKET` 重新投递 message。

## 12. 工具能力验证

真实验证 stock_data_collector 盘中能力：

```bash
intel-agent --config config/intelligence_collector.yaml tools verify-capabilities
```

配置：

```yaml
capability_verification:
  stock_data_collector:
    ticker: "600519.SH"
    start_date: "2026-06-01"
    end_date: "2026-06-01"
    frequencies: ["5m", "15m"]
    fallback_order: ["5m", "15m", "none"]
    unverified_default_frequency: "15m"
```

每个频率会执行：

1. `fetch historical-bars --frequency 5m/15m`
2. `query bars --frequency 5m/15m`
3. 只有 `query_rows > 0` 且质量满足阈值时，才标记 `usable: true`。

能力结果写入 state DB 的 `tool_capabilities`，并发布 `capability.validation.result` message。

## 13. 盘中行情特征

盘中任务默认行为：

```yaml
intraday:
  use_query_bars_after_fetch: true
  query_bars_trading_ready: false
```

流程：

1. 根据能力验证选择 5m、15m 或 none。
2. 调用 stock_data_collector fetch。
3. 再调用 `query bars` 读取已落库行情。
4. 聚合到 Agent 配置的 bucket，例如 10m。
5. 写入 `market_features`。
6. 超过 `market_features.abnormality_ticket_threshold` 时，生成 `MARKET_FEATURE_TICKET`。

若只有 15m 可用，特征中会记录：

```json
{
  "degraded_from": "10m_to_15m",
  "source_frequency": "15m"
}
```

## 14. 质量闸门

配置：

```yaml
quality:
  minimum_quality_for_public_pool: 0.80
  critical_conflict_action: quarantine_and_alert
  high_conflict_action: accept_with_review
```

stock_data 质量检查包括：

- `status`
- `errors[].error_code`
- `persistence.saved`
- `provider_results[]`
- `quality_report.data_quality_score`
- `quality_report.conflicts[].severity`
- `rows_fetched`
- `inline_bars_count`

`data_quality_score < minimum_quality_for_public_pool` 时，数据会被降级处理，不进入可交易分析层。

## 15. 真实工具要求

### MIC

需要满足：

- `from mic.api import AnalystAPI` 可用。
- MIC 自己的搜索、模型、OpenClaw gateway、API Key 已配置。
- MIC 不保存网页原文，只保存链接和结构化结果。

### stock_data_collector

需要满足：

- `python -m stock_data_ingestion.cli` 可运行。
- 如设置 `tools.stock_data_collector.config_dir`，必须是绝对路径或由 `workspace_root` 正确解析出的路径。
- `TUSHARE_TOKEN` 等凭证已配置。
- 资金流需要有效 `EASTMONEY_COOKIE`。
- 盘中能力必须先跑 `tools verify-capabilities`。

## 16. 内部读取 CLI

Reader 仅用于内部测试、调试和 CLI。多 Agent 服务化时，其他 Agent 应通过消息机制查询，不应直接调用 Reader。

```bash
intel-agent --config config/intelligence_collector.yaml read collection-status --demand-id demand_xxx
intel-agent --config config/intelligence_collector.yaml read events --ticker 300750.SZ
intel-agent --config config/intelligence_collector.yaml read market-features --ticker 300750.SZ --window 10m
intel-agent --config config/intelligence_collector.yaml read data-quality --status open
intel-agent --config config/intelligence_collector.yaml read capabilities
```

## 17. HTML / JSON 日报

```bash
intel-agent --config config/intelligence_collector.yaml report daily --trade-date 2026-06-11
```

输出：

```text
reports/collection_report_20260611.html
reports/collection_report_20260611.json
```

日报包括：

- 任务数量。
- 工具运行统计。
- Ticket 统计。
- 能力验证结果。
- 熔断状态。
- 结构化事件 Top N。
- 行情特征 Top N。
- 数据质量问题。
- 覆盖缺口。
- 故障 Ticket。

同时生成 `COLLECTION_REPORT_TICKET`。

## 18. 常用命令顺序

```bash
# 1. 安装
python -m pip install -e .

# 2. 修改 config/intelligence_collector.yaml 的 openclaw.model.primary

# 3. 初始化三库
intel-agent --config config/intelligence_collector.yaml init-db

# 4. 校验 OpenClaw 模型
intel-agent --config config/intelligence_collector.yaml openclaw validate-model

# 5. 验证 stock_data 盘中能力
intel-agent --config config/intelligence_collector.yaml tools verify-capabilities

# 6. 注册 Demand
intel-agent --config config/intelligence_collector.yaml demand register \
  --file examples/demands/held_sellable_10m.json \
  --activate

# 7. Runtime tick
intel-agent --config config/intelligence_collector.yaml runtime tick \
  --now "2026-06-11T10:30:00+08:00"

# 8. 运行 Agent
intel-agent --config config/intelligence_collector.yaml agent run-until-idle --max-messages 100

# 9. 查看状态
intel-agent --config config/intelligence_collector.yaml agent status

# 10. 生成日报
intel-agent --config config/intelligence_collector.yaml report daily --trade-date 2026-06-11
```

## 19. 测试

离线内部测试：

```bash
PYTHONPATH=src pytest -q
```

当前测试覆盖：

- Demand 注册、生命周期、版本元数据。
- Ticket / Message 投递。
- Queue lease / ack / nack / dead-letter / retry。
- Planner cadence 与 market phase 约束。
- split store 基础路径。
- OpenClaw model placeholder 硬拦截。
- tool path workspace_root 解析。
- QualityGate 阈值。
- Recovery 基础逻辑。
- Circuit breaker。
- Market feature 聚合和降级标记。
- HTML/JSON 日报。

真实工具 E2E 不在 pytest 默认路径中运行，因为它需要真实 MIC、真实 stock_data_collector、API Key、Tushare Token 和本地交易数据环境。

## 20. 打包与安全

运行产物应位于：

```text
data/
logs/
reports/
```

源码交付应避免混入：

```text
.env
logs/
reports/
__pycache__/
.pytest_cache/
*.pyc
*.db-wal
*.db-shm
*.sqlite-wal
*.sqlite-shm
```

如果必须交付本地 SQLite 数据文件，必须放在 `data/` 目录下，例如：

```text
data/mic.db
```

安全纪律：

- 不提交真实 `.env`。
- 不提交真实 Token、Cookie、账号、密码。
- 不在日志、Memory 或 Ticket 中写入完整 Cookie。
- 不绕过验证码、反爬或付费墙。
- 不把低可信来源写成高可信事实。

## 21. OpenClaw Artifact

渲染 OpenClaw Agent / Skill：

```bash
intel-agent --config config/intelligence_collector.yaml openclaw render-artifacts --output-dir build/openclaw
```

输出：

```text
build/openclaw/openclaw_agent/agent.md
build/openclaw/openclaw_agent/skills/intelligence-collector/SKILL.md
build/openclaw/openclaw_agent/openclaw_config_patch.json
```

Artifact 中会声明：

- 当前 agent_id。
- 当前 OpenClaw model。
- state / bus / data SQLite 路径。
- 本 Agent 的运行边界。
- 常用 CLI 工作流。

