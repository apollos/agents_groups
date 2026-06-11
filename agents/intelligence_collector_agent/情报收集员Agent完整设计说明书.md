# 情报收集员 Agent 完整设计说明书 V0.3

项目：Agent 交易公司 / 多 Agent A 股量化交易助手  
文档类型：设计评审稿  
版本：V0.3  
日期：2026-06-11  
状态：根据最新设计决策修订

---

## 0. V0.3 关键修订结论

本版根据最新确认事项，对 V0.2 做如下固化：

| 决策项 | V0.3 设计结论 |
|---|---|
| 第一版存储 | 第一版使用 SQLite，开启 WAL；采集数据库、Demand Registry、Ticket、消息队列、Checkpoint、运行台账均先落 SQLite。 |
| 消息机制 | 必须使用消息队列机制。第一版实现 `SQLiteMessageQueue`，对外暴露统一 `MessageQueue` 接口；后续可替换 Redis Streams / RabbitMQ / Kafka，而不改变 Agent 业务逻辑。 |
| Ticket 与 Message 关系 | Ticket 是业务对象，Message 是投递载体。Agent 不直接轮询业务表，而是消费队列中的 message，message payload 指向 Ticket 或 Demand。 |
| 工具调用 | 不再使用 mock 作为 Agent 集成测试主路径。测试环境直接调用真实 `market_intelligence_collector` 和真实 `stock_data_collector`，但使用测试配置、小预算、测试 SQLite 和测试输出目录。 |
| 能力验证 | 必须实现 `CAPABILITY_VALIDATION` 流程，尤其验证 stock_data_collector 的盘中 5m/15m/realtime_quote 能力，验证结果写入 `tool_capabilities`，供调度降级使用。 |
| 读取情报 | 读取工具只作为内部测试、调试、CLI 查询和验收工具。多 Agent 服务化时，Agent 之间通过消息和 Ticket 完成查询请求与响应，不直接互相调用读取工具。 |
| 采集日报 | 采集日报必须有可读形式。第一版生成 HTML，同时保留 JSON 结构化日报。 |
| 人工入口 | 第一版不做人工 UI，使用 CLI 注册、激活、暂停、恢复、取消 Demand。 |
| 查询频率 | 所有频率、预算、阈值、降级策略、告警策略都放入 YAML config。我们讨论的 10 分钟、30–60 分钟、4 小时等只作为默认值。 |
| 盘中最小粒度 | 默认 10 分钟调度 bucket，但必须可配置。底层数据源没有原生 10m 时，可 5m 聚合、15m 降级或降级为交易状态检查。 |

---

## 1. 系统定位

情报收集员 Agent 是多 Agent A 股投研与交易辅助系统的数据入口和采集执行内核。

它负责根据 Demand Registry、持仓系统、股票池、交易日历和事件触发，消费 Runtime Controller 投递到消息队列的采集消息，调用两个真实工具完成数据采集：

1. `market_intelligence_collector`：采集行业、公司、政策、公告、舆情、风险、关系、催化剂、覆盖缺口等非结构化情报，并结构化落库。
2. `stock_data_collector`：采集 A 股 / 港股通结构化行情、交易状态、估值、财务、资金流、复权、公司行动，并保存 raw、质量评分和冲突记录。

情报收集员不做投资决策，不输出买卖建议，不下单，不决定仓位，不绕过分析员矩阵和风控。

---

## 2. 总体架构

### 2.1 第一版架构

```text
人工 CLI / 持仓系统 / 股票池系统 / 分析员请求 / 风控请求 / 定时规则 / 事件触发
        ↓
Demand Registry, SQLite
        ↓
Runtime Controller
        ↓
SQLiteMessageQueue, durable queue
        ↓
Intelligence Collection Agent
        ↓
真实 market_intelligence_collector / 真实 stock_data_collector
        ↓
公共数据池 / 事件库 / 行情特征库 / 采集台账 / tool_capabilities
        ↓
Ticket + Message
        ↓
分析员矩阵 / 风控 Agent / 总分析师 Agent / 负责人
```

### 2.2 未来服务化架构

```text
Demand Service
        ↓
Message Broker, Redis Streams / RabbitMQ / Kafka
        ↓
Agent Services
        ↓
Shared Data Services / Query Services
        ↓
Ticket Messages / Report Messages / Alert Messages
```

第一版虽然使用 SQLite，但代码层必须抽象出 `MessageQueue` 接口，避免业务逻辑绑定 SQLite 实现。

---

## 3. Ticket 与 Message 的边界

### 3.1 核心定义

| 对象 | 作用 | 是否持久化 | 示例 |
|---|---|---:|---|
| Demand | 长期采集意图 | 是 | 每天跟踪当前持仓，盘中 10 分钟采集。 |
| Ticket | 业务任务或业务事实 | 是 | `COLLECTION_TASK_TICKET`、`EVENT_TICKET`。 |
| Message | 队列投递载体 | 是 | 把某个 Ticket 投递给情报收集员。 |
| Run | 实际执行记录 | 是 | 某次 MIC 或 stock_data 工具调用。 |
| Checkpoint | Agent 恢复包 | 是 | 当前任务、待处理消息、下一次唤醒。 |

### 3.2 为什么需要同时有 Ticket 和 Message

Ticket 负责业务语义，Message 负责可靠投递。

| 问题 | Ticket 解决 | Message 解决 |
|---|---|---|
| 这是什么任务 | 是 | 否 |
| 谁创建、谁接收、优先级、证据链 | 是 | 部分 |
| 是否可审计、可回放 | 是 | 部分 |
| 如何投递给 Agent | 否 | 是 |
| 如何 ack / retry / dead-letter | 否 | 是 |
| 多 Agent 消费如何解耦 | 否 | 是 |

因此第一版规则是：

```text
Demand 编译生成 Ticket；Ticket 写库；Runtime Controller 为 Ticket 创建 Message；Agent 消费 Message 后读取 Ticket 执行；执行结果更新 Ticket 并发出新的 Message。
```

---

## 4. SQLite 存储设计

### 4.1 第一版存储原则

第一版使用 SQLite + WAL，优先满足单机原型、真实工具接入、实时模拟盘初期和可恢复运行。

```yaml
sqlite:
  path: data/agent_trade.sqlite
  enable_wal: true
  busy_timeout_ms: 5000
  foreign_keys: true
  journal_size_limit_bytes: 67108864
```

### 4.2 核心表

| 表 | 用途 |
|---|---|
| `collection_demands` | Demand 当前版本摘要与状态。 |
| `collection_demand_versions` | Demand 历史版本，append-only。 |
| `tickets` | Ticket 主表。 |
| `ticket_events` | Ticket 状态变化流水，append-only。 |
| `message_queue` | durable queue 主表。 |
| `message_attempts` | message 消费尝试记录。 |
| `dead_letters` | 超过重试次数或不可恢复消息。 |
| `collection_tasks` | 原子采集任务。 |
| `collection_runs` | 工具调用与执行记录。 |
| `structured_events` | 结构化事件。 |
| `market_features` | 10 分钟或配置窗口行情特征。 |
| `data_quality_issues` | 数据质量问题。 |
| `coverage_gaps` | 覆盖缺口。 |
| `tool_capabilities` | 工具能力验证结果。 |
| `agent_sessions` | Agent Session。 |
| `agent_checkpoints` | Agent checkpoint。 |
| `runtime_heartbeats` | 心跳。 |
| `circuit_breakers` | 工具熔断状态。 |
| `daily_collection_reports` | JSON 日报。 |
| `report_artifacts` | HTML/Markdown/PDF 等可读报告路径。 |

### 4.3 SQLite 并发约束

SQLite 第一版可以支撑单机多进程轻量并发，但要遵守：

1. 开启 WAL。
2. 写操作保持短事务。
3. Message lease、ack、retry 必须在事务内完成。
4. Agent 消费者数量第一版不宜过多，默认 1–3 个 worker。
5. 大字段不进 SQLite，使用 `payload_ref`、`raw_ref`、`artifact_path`。
6. 真实生产扩容时替换消息队列和主库，不改业务 schema 语义。

---

## 5. Message Queue 设计

### 5.1 第一版实现：SQLiteMessageQueue

第一版使用 SQLite 实现 durable queue，但对业务暴露接口：

```python
class MessageQueue:
    def publish(self, message: QueueMessage) -> str: ...
    def lease(self, consumer_id: str, topics: list[str], max_messages: int) -> list[QueueMessage]: ...
    def ack(self, message_id: str, consumer_id: str) -> None: ...
    def nack(self, message_id: str, consumer_id: str, reason: str, retryable: bool) -> None: ...
    def extend_lease(self, message_id: str, consumer_id: str, seconds: int) -> None: ...
    def move_to_dead_letter(self, message_id: str, reason: str) -> None: ...
```

未来可替换为：

| 实现 | 适用阶段 |
|---|---|
| `SQLiteMessageQueue` | MVP / 单机真实工具测试 / 早期实时模拟。 |
| `RedisStreamsMessageQueue` | 多 Agent 服务化初期。 |
| `RabbitMQMessageQueue` | 需要成熟 ack、routing、dead-letter。 |
| `KafkaMessageQueue` | 高吞吐事件流与长周期 replay。 |

### 5.2 Message Topic

| Topic | 消费者 | 用途 |
|---|---|---|
| `demand.registered` | Runtime Controller | 新 Demand 注册后触发编译。 |
| `demand.changed` | Runtime Controller | Demand 变更后重新编译或取消旧任务。 |
| `collection.request` | Intelligence Collector | 采集请求。 |
| `collection.task` | Intelligence Collector | 原子采集任务。 |
| `collection.result` | Runtime / Report Builder | 采集结果。 |
| `event.created` | 分析员矩阵 / 风控 | 结构化事件。 |
| `market_feature.created` | G3 / 风控 | 行情特征异常。 |
| `data_quality.issue` | 数据维护 / 负责人 | 数据质量问题。 |
| `fault.created` | Runtime / 负责人 | 工具或系统故障。 |
| `coverage_gap.created` | Runtime / 情报员 | 覆盖缺口补采。 |
| `capability.validation.request` | Intelligence Collector | 工具能力验证请求。 |
| `capability.validation.result` | Runtime / 配置维护 | 能力验证结果。 |
| `report.collection_daily` | 负责人 / 总分析师 | 采集日报。 |
| `query.intelligence.request` | Intelligence Query Service | 多 Agent 查询情报请求。 |
| `query.intelligence.response` | 请求方 Agent | 查询响应。 |
| `checkpoint.created` | Runtime Controller | Agent 检查点。 |

### 5.3 Message Schema

```json
{
  "message_id": "msg_20260611_103000_000001",
  "topic": "collection.task",
  "schema_version": "message.v1",
  "status": "ready",
  "priority": "high",
  "created_at": "2026-06-11T10:30:00+08:00",
  "available_at": "2026-06-11T10:30:00+08:00",
  "expires_at": "2026-06-11T10:45:00+08:00",
  "producer": "runtime_controller",
  "consumer_group": "intelligence_collector",
  "payload_type": "ticket_ref",
  "payload_ref": "db://tickets/ticket_20260611_103000_000101",
  "correlation_id": "corr_collection_20260611_000001",
  "parent_message_id": null,
  "idempotency_key": "message:collection.task:ticket_20260611_103000_000101",
  "attempt_count": 0,
  "max_attempts": 3,
  "lease_owner": null,
  "lease_until": null,
  "trace": {
    "demand_id": "demand_20260611_000001",
    "ticket_id": "ticket_20260611_103000_000101",
    "target_id": "company_300750",
    "ticker": "300750.SZ"
  }
}
```

### 5.4 Message 状态机

```text
ready
  ↓ lease
in_progress
  ↓ ack
acked

in_progress
  ↓ nack retryable + attempts < max
ready

in_progress
  ↓ nack non_retryable / attempts >= max
failed / dead_letter

ready / in_progress
  ↓ expires_at < now
expired
```

### 5.5 Lease 与恢复

1. Agent 消费 message 前必须获得 lease。
2. lease 有超时时间，例如默认 5 分钟。
3. 长任务必须定期 `extend_lease`。
4. Agent 崩溃后，Runtime Controller 将过期 lease 的 message 重新置为 `ready`，或根据幂等键检查结果已完成则 ack。
5. 重试次数超过上限进入 `dead_letters` 并生成 `FAULT_TICKET`。

---

## 6. Demand Registry 设计

### 6.1 Demand 是长期意图

Demand Registry 保存“系统长期要做什么”，不直接等于某一次工具调用。

示例：

```text
每天盘中对当前可卖持仓每 10 分钟采集轻量行情快照；每 30–60 分钟做黑天鹅扫描；盘后做完整日频刷新；所有频率和阈值以 YAML 配置为准。
```

### 6.2 Demand 下发方式

第一版只支持 CLI，不做人工 UI。

```bash
agent-trade demand validate --file demands/held_sellable_10m.yaml --env test
agent-trade demand register --file demands/held_sellable_10m.yaml --env test --activate
agent-trade demand list --env test --status active
agent-trade demand get --env test --demand-id demand_20260611_000001
agent-trade demand suspend --env test --demand-id demand_20260611_000001
agent-trade demand resume --env test --demand-id demand_20260611_000001
agent-trade demand cancel --env test --demand-id demand_20260611_000001
agent-trade demand compile --env test --demand-id demand_20260611_000001 --as-of 2026-06-11T10:30:00+08:00
```

注册 Demand 后会：

```text
1. 校验 schema。
2. 写入 collection_demands 和 collection_demand_versions。
3. 发布 demand.registered message。
4. Runtime Controller 消费该 message。
5. Runtime Controller 根据 config、交易日历、持仓、股票池编译 collection.request / collection.task。
```

### 6.3 Demand YAML 示例

```yaml
schema_version: demand.v1
demand_id: demand_20260611_held_sellable_001
demand_type: intraday_monitoring
source_type: manual
status: active
created_by: owner
owner: owner
priority: high
active_from: 2026-06-11
active_to: null
market: A_SHARE
timezone: Asia/Shanghai

target_scope:
  scope_type: dynamic_pool
  pool_layers:
    - current_holding
  filters:
    sellability: sellable
    exclude_st: true
    exclude_suspended: false

targets: []

cadence_profile: held_sellable_default

focus:
  mic:
    - risk
    - customer_change
    - supply_chain
    - policy
  stock_data:
    - intraday_light_snapshot
    - trading_status
    - post_close_full_refresh

alert_policy:
  notify_on: [P0, P1]
  notify_owner: true
  notify_channels: [message, html_report]

output_contract:
  emit_event_ticket: true
  emit_market_feature_ticket: true
  emit_data_quality_ticket: true
  emit_collection_result_ticket: true
  write_public_data_pool: true

test_mode: false
idempotency_key: demand:held_sellable_default:current_holding:20260611:v1
```

---

## 7. YAML 配置化设计

所有频率、预算、阈值、降级策略都必须配置化。代码中只能保留默认加载逻辑，不能硬编码业务频率。

### 7.1 推荐配置文件结构

```text
config/
  intelligence_collector.yaml
  demand_profiles.yaml
  cadence_profiles.yaml
  market_calendar.yaml
  message_queue.yaml
  database.yaml
  tool_profiles.yaml
  quality_gate.yaml
  capability_validation.yaml
  report.yaml
  alerts.yaml
```

### 7.2 频率配置示例

```yaml
cadence_profiles:
  held_sellable_default:
    description: 当前持仓且可卖，默认高优先级监控
    market_snapshot:
      enabled: true
      bucket_size: 10m
      during_market: true
      allow_lunch_break: false
      stale_after: 15m
    mic_black_swan_scan:
      enabled: true
      interval: 45m
      budget_profile: black_swan_small
    post_close_full_refresh:
      enabled: true
      after_time: "20:30"

  held_t1_locked_default:
    description: 当前持仓但 T+1 未解锁
    market_snapshot:
      enabled: true
      bucket_size: 30m
      during_market: true
    mic_black_swan_scan:
      enabled: true
      interval: 60m
    post_close_full_refresh:
      enabled: true
      after_time: "20:30"

  trading_candidate_default:
    description: 交易候选池
    market_snapshot:
      enabled: true
      bucket_size: 20m
      during_market: true
    mic_black_swan_scan:
      enabled: true
      interval: 60m
    candidate_full_snapshot:
      enabled: true
      trigger: on_demand

  watchlist_default:
    description: 研究观察池
    market_snapshot:
      enabled: true
      bucket_size: 60m
    mic_regular_scan:
      enabled: true
      interval: 4h

  base_pool_default:
    description: 基础可交易池
    market_snapshot:
      enabled: false
    daily_refresh:
      enabled: true
      after_time: "20:30"
```

### 7.3 工具预算配置示例

```yaml
budget_profiles:
  black_swan_small:
    max_queries: 8
    max_links_to_read: 4
    max_model_calls: 3
    timeout_seconds: 120

  company_regular_medium:
    max_queries: 20
    max_links_to_read: 10
    max_model_calls: 8
    timeout_seconds: 300

  company_deep_large:
    max_queries: 60
    max_links_to_read: 30
    max_model_calls: 20
    timeout_seconds: 900
```

### 7.4 阈值配置示例

```yaml
market_feature_thresholds:
  default:
    emit_market_feature_ticket_if:
      abnormality_score_gte: 0.75
      return_abs_10m_gte: 0.02
      amount_ratio_vs_20d_same_bucket_gte: 3.0
      distance_to_limit_up_lte: 0.015
      distance_to_limit_down_lte: 0.015
    trigger_risk_review_if:
      held_sellable_negative_return_10m_lte: -0.025
      hit_limit_down: true
      trading_status_abnormal: true
```

---

## 8. 盘中 10 分钟采集设计

### 8.1 默认规则

默认盘中最小调度粒度为 10 分钟，但由 config 决定。

```yaml
intraday:
  default_bucket_size: 10m
  timezone: Asia/Shanghai
  continuous_auction_windows:
    - ["09:30", "11:30"]
    - ["13:00", "15:00"]
  allow_lunch_break_collection: false
  first_bucket_policy: wait_until_complete
  last_bucket_policy: collect_before_close_if_complete
```

### 8.2 底层频率兼容

stock_data_collector 的 README 显示其请求模型支持 `1m / 5m / 15m / 30m / 60m / 1d / 1w / 1mo / realtime`，但第一阶段默认目标仍是日频盘后数据，分钟线和 realtime_quote 不能直接假设稳定可用。因此第一版必须通过能力验证决定实际使用路径。

```yaml
intraday_data_fallback_order:
  - native_10m_snapshot_if_available
  - aggregate_5m_to_10m
  - fallback_15m_feature
  - trading_status_only
  - skip_and_emit_capability_gap
```

### 8.3 10 分钟任务只采轻量字段

盘中 10 分钟任务不刷新全量财务、估值和长窗口历史数据。

| 字段 | 用途 |
|---|---|
| 当前价 / 最近价 | 盘中价格状态。 |
| 涨跌幅 | 日内涨跌。 |
| 成交量 / 成交额 | 活跃度。 |
| 换手率 | 筹码活跃。 |
| VWAP / 均价偏离 | 交易强弱。 |
| 涨跌停距离 | 可交易性和风险。 |
| 停牌 / ST / 涨跌停 | 交易状态。 |
| 行业相对强弱 | 个股是否强于行业。 |
| 成交额相对历史同时间桶倍数 | 异动判断。 |

### 8.4 10 分钟行情特征

```json
{
  "record_type": "market_feature",
  "schema_version": "market_feature.v1",
  "feature_window": "10m",
  "ticker": "300750.SZ",
  "target_id": "company_300750",
  "bucket_start": "2026-06-11T10:30:00+08:00",
  "bucket_end": "2026-06-11T10:40:00+08:00",
  "price_features": {
    "return_10m": 0.012,
    "relative_to_industry_10m": 0.008,
    "position_in_intraday_range": 0.76
  },
  "volume_features": {
    "amount_ratio_10m_vs_20d_same_time": 2.4
  },
  "trend_features": {
    "above_vwap": true,
    "break_recent_high": false
  },
  "tradability_features": {
    "is_suspended": false,
    "is_st": false,
    "hit_limit_up": false,
    "hit_limit_down": false
  },
  "abnormality_score": 0.71,
  "data_quality": 0.86,
  "source_refs": ["stock_data_request_req_xxx"],
  "summary_cn": "近10分钟上涨1.2%，强于行业0.8个百分点，成交额为过去20日同时间段均值2.4倍。"
}
```

---

## 9. 工具能力验证

### 9.1 为什么必须实现

stock_data_collector 已有分钟线和 realtime_quote 的请求模型与表结构，但 README 明确说明默认目标是日频盘后数据；分钟线能力依赖具体 provider，CLI 也没有把 realtime_quote 作为第一阶段完整 fetch 子命令暴露。因此第一版必须实现能力验证，不能直接假设盘中能力可用。

### 9.2 验证时机

| 时机 | 动作 |
|---|---|
| 系统启动 | 验证 CLI、配置、SQLite、工具版本。 |
| 每个交易日盘前 | 验证 stock_data_collector 凭证、Eastmoney Cookie、分钟线能力、日频 fetch。 |
| 首次启用某个 provider | 对 provider 执行小样本验证。 |
| 工具升级后 | 重新验证所有关键能力。 |
| 连续失败后 | 标记 unstable 并触发熔断或降级。 |

### 9.3 能力验证 Demand / Message

能力验证也走消息队列：

```text
Runtime Controller 发布 capability.validation.request
  ↓
Intelligence Collector 消费
  ↓
真实调用 stock_data_collector / MIC 小样本任务
  ↓
写入 tool_capabilities
  ↓
发布 capability.validation.result
```

### 9.4 capability_validation.yaml 示例

```yaml
capability_validation:
  enabled: true
  run_on_startup: true
  run_pre_market: true
  pre_market_time: "09:05"
  test_tickers:
    a_share:
      - 600519.SH
      - 000001.SZ
  stock_data:
    config_dir: tools/stock_data_collector/config
    checks:
      cli_available: true
      init_db: false
      eastmoney_cookie: true
      trading_status: true
      historical_bars_1d: true
      historical_bars_5m_intraday: true
      historical_bars_15m_intraday: true
      realtime_quote_python_layer: true
      query_meta_summary: true
    timeout_seconds: 120
    max_retries: 1
  mic:
    enabled: true
    target_id: company_300750
    focus: [risk]
    time_window: 7d
    budget_profile:
      max_queries: 2
      max_links_to_read: 1
      max_model_calls: 1
```

### 9.5 tool_capabilities 记录

```json
{
  "capability_id": "cap_stock_data_20260611_090500",
  "tool_name": "stock_data_collector",
  "tool_version": "0.3.0",
  "checked_at": "2026-06-11T09:05:00+08:00",
  "environment": "test|prod",
  "capabilities": {
    "cli_available": "available",
    "eastmoney_cookie": "available|expired|missing",
    "trading_status": "available",
    "historical_bars_1d": "available",
    "historical_bars_5m_intraday": "available|unavailable|unstable|unknown",
    "historical_bars_15m_intraday": "available|unavailable|unstable|unknown",
    "realtime_quote_python_layer": "available|unavailable|unstable|unknown"
  },
  "recommended_intraday_mode": "aggregate_5m_to_10m|fallback_15m_feature|trading_status_only",
  "errors": [],
  "raw_run_refs": ["db://collection_runs/run_xxx"],
  "valid_until": "2026-06-11T15:30:00+08:00"
}
```

### 9.6 降级规则

| 验证结果 | 盘中处理 |
|---|---|
| 5m 可用 | 默认 5m 聚合为 10m 特征。 |
| 15m 可用、5m 不可用 | 默认降级 15m 特征，并在特征中标记 `degraded_from=10m_to_15m`。 |
| 仅 trading_status 可用 | 只采交易状态，不生成行情特征，发能力缺口。 |
| 关键能力 unknown | 先执行小样本验证，再决定执行或跳过。 |
| 连续失败 | 熔断该能力，生成 `DATA_QUALITY_TICKET` 或 `FAULT_TICKET`。 |

---

## 10. 真实工具测试策略

### 10.1 不使用 mock 的原则

本版接受“工具已经单独测试过”的前提，Agent 集成测试不再以 mock adapter 作为主路径。第一版测试采用真实工具，但必须隔离环境。

### 10.2 测试环境要求

| 项 | 要求 |
|---|---|
| SQLite | 使用 `data/test/agent_trade_test.sqlite`，不能写生产库。 |
| stock_data_collector | 使用测试 config-dir，raw/parquet/log 指向测试目录。 |
| MIC | 使用测试数据库或测试 target，预算最小化。 |
| 外部费用 | 测试配置中设置极小预算，避免大规模搜索和模型调用。 |
| 凭证 | 使用真实凭证，但不能写入日志、报告或消息 payload。 |
| 通知 | 测试环境不发真实外部通知，只写 message / report。 |
| 幂等 | 每个测试 demand 使用固定 idempotency_key，重复执行应不重复落库。 |

### 10.3 测试分层

| 测试 | 是否调用真实工具 | 目的 |
|---|---:|---|
| Schema 测试 | 否 | 校验 Demand、Ticket、Message、Config。 |
| Repository 测试 | 否 | 校验 SQLite 表、幂等、事务。 |
| Queue 测试 | 否 | 校验 publish、lease、ack、retry、dead-letter。 |
| Capability Validation 测试 | 是 | 验证 stock_data / MIC 实际能力。 |
| Agent Integration 测试 | 是 | 真实消费 message 并真实调用工具。 |
| E2E Smoke 测试 | 是 | Demand → Message → Agent → Tool → Result → HTML Report。 |
| 崩溃恢复测试 | 是或可半真实 | 真实落库后崩溃恢复，不重复工具调用。 |

### 10.4 最小真实 E2E 命令

```bash
agent-trade db init --env test --reset

agent-trade demand register \
  --env test \
  --file tests/fixtures/demands/held_sellable_default.yaml \
  --activate

agent-trade runtime tick \
  --env test \
  --now "2026-06-11T09:05:00+08:00" \
  --run-capability-validation

agent-trade runtime tick \
  --env test \
  --now "2026-06-11T10:30:00+08:00"

agent-trade agent run-once \
  --env test \
  --agent intelligence_collector

agent-trade report collection-daily \
  --env test \
  --trade-date 2026-06-11 \
  --format html
```

注意：不再提供 `--mock-tools` 作为主测试路径。若将来保留，也只能作为离线开发便利功能，不能作为验收依据。

---

## 11. 多 Agent 服务化时的读取机制

### 11.1 V0.3 调整

V0.2 中的 `IntelligenceReaderTool` 仍然保留，但定位调整为：

> 内部测试、调试、CLI 查询、验收和人工审查工具。

多 Agent 服务运行时，分析员、风控、总分析师不直接调用情报员内部工具，而是通过消息队列发出查询请求。

### 11.2 消息化查询流程

```text
分析员 Agent
  ↓ publish query.intelligence.request
Intelligence Query Service / 情报查询服务
  ↓ 读取 SQLite / MIC Repository / stock_data QueryService
  ↓ publish query.intelligence.response
分析员 Agent
  ↓ 根据 response 中的 evidence_refs 进行分析
```

### 11.3 Query Request Ticket

```json
{
  "ticket_type": "INTELLIGENCE_QUERY_REQUEST_TICKET",
  "source_agent": "G2_industry_event_A",
  "target_agent_group": "intelligence_query_service",
  "priority": "normal",
  "summary_cn": "请求读取 300750.SZ 近30天供应链、政策和风险事件。",
  "payload": {
    "query_type": "recent_events",
    "target": {
      "target_id": "company_300750",
      "ticker": "300750.SZ"
    },
    "filters": {
      "since": "30d",
      "event_types": ["supply_chain", "policy_regulation", "legal_compliance"],
      "min_confidence": 0.6,
      "minimum_data_quality": 0.8
    },
    "limit": 50,
    "include_evidence_refs": true
  },
  "idempotency_key": "query:recent_events:company_300750:30d:G2A:20260611"
}
```

### 11.4 Query Response Ticket

```json
{
  "ticket_type": "INTELLIGENCE_QUERY_RESPONSE_TICKET",
  "parent_ticket_id": "ticket_query_req_xxx",
  "source_agent": "intelligence_query_service",
  "target_agent_group": "G2_industry_event_A",
  "summary_cn": "返回 300750.SZ 近30天相关事件 12 条，其中高置信风险 2 条。",
  "payload_ref": "db://query_results/query_result_xxx",
  "evidence_refs": ["event_xxx", "source_link_xxx", "feature_xxx"],
  "idempotency_key": "query_response:ticket_query_req_xxx:v1"
}
```

### 11.5 CLI 读取仍保留

用于测试和人工检查：

```bash
agent-trade intelligence read events --env test --target-id company_300750 --since 30d
agent-trade intelligence read market-features --env test --ticker 300750.SZ --date 2026-06-11 --window 10m
agent-trade intelligence read collection-status --env test --demand-id demand_20260611_held_sellable_001
agent-trade intelligence read ticket-chain --env test --correlation-id corr_collection_20260611_000001
```

---

## 12. 质量闸门

### 12.1 stock_data_collector 质量闸门

每条 `fetch` 输出的 `StockDataResponse` 必须检查：

1. `status`
2. `errors[].error_code`
3. `provider_results[]`
4. `persistence.saved`
5. `quality_report.data_quality_score`
6. `quality_report.conflicts[].severity`
7. `raw_payload_ids` / `raw_payload_refs`

处理规则：

| 条件 | 处理 |
|---|---|
| `success` + `saved=true` + 无 high/critical | 可进入公共数据池。 |
| `partial_success` 且 canonical provider 成功 | 可用但标记降级，日报列出。 |
| `persistence.saved=false` | 不可用，生成 `DATA_QUALITY_TICKET`。 |
| `high` 冲突 | 生成数据质量 Ticket，进入复核。 |
| `critical` 冲突 | 隔离，不进入 trading-ready。 |
| `TOKEN_MISSING` / `AUTH_FAILED` / `PERMISSION_DENIED` | P0/P1，负责人处理。 |
| `RATE_LIMITED` | 降速、分批、重试。 |
| `EMPTY_RESULT` | 结合交易日、停牌、事件稀疏判断，不单独算失败。 |

### 12.2 MIC 质量闸门

| 条件 | 处理 |
|---|---|
| 官方公告 / 监管来源 | 可作为高可信来源。 |
| 多来源交叉印证 | 提高置信度。 |
| 单一媒体来源 | 标记 single_source，降权。 |
| C 级来源 | 不能单独触发买入，只能辅助观察。 |
| 反爬 / 验证码 | 标记 failed，不混入正常结果。 |
| coverage_gap | 生成覆盖缺口消息。 |
| 模型输出冲突 | 仲裁或人工复核。 |

---

## 13. 故障分级

| 级别 | 示例 | 处理 |
|---|---|---|
| P0 | SQLite 写入失败、raw 保存失败、Tushare 鉴权失败、critical 冲突、持仓关键数据完全缺失 | 停止相关任务，发 `fault.created`，生成 `FAULT_TICKET`。 |
| P1 | high 冲突、可卖持仓黑天鹅命中、主源限频、核心数据 partial_success | 降级继续，通知负责人。 |
| P2 | 辅源失败、个别 MIC 链接反爬、资金流 Cookie 失效但主流程可继续 | 记录到日报，不立即打扰。 |
| P3 | 空结果、重复幂等跳过、无公司行动、无新公告 | 只记日志。 |

---

## 14. 长期运行与恢复

### 14.1 必须支持

1. heartbeat。
2. message lease。
3. 幂等键。
4. checkpoint。
5. session rotation。
6. circuit breaker。
7. dead-letter。
8. 崩溃恢复。
9. HTML/JSON 日报。
10. 每日归档备份。

### 14.2 Agent 主循环

```text
1. heartbeat
2. lease message
3. load ticket / demand / payload
4. check config and market phase
5. validate tool capability
6. execute real tool call
7. parse result
8. run quality gate
9. persist structured output
10. emit downstream ticket and message
11. ack message
12. checkpoint
```

### 14.3 崩溃恢复流程

```text
1. Runtime Controller 发现 heartbeat timeout。
2. 查询 agent_sessions 和最新 checkpoint。
3. 找出 in_progress 且 lease_expired 的 message。
4. 根据 idempotency_key 检查任务是否已落库。
5. 已落库但未 ack：补发结果 Ticket 或 ack。
6. 未落库且可重试：重新置为 ready。
7. 超过重试上限：进入 dead_letter，生成 FAULT_TICKET。
8. 从 Demand、Ticket、Message、Checkpoint 重建上下文。
9. 继续执行。
```

---

## 15. HTML 采集日报

### 15.1 输出形式

每天收盘后生成两份报告：

| 文件 | 用途 |
|---|---|
| `collection_report_YYYYMMDD.json` | 结构化日报，供 Agent 和程序读取。 |
| `collection_report_YYYYMMDD.html` | 人类可读日报，供负责人和总分析师查看。 |

### 15.2 HTML 报告内容

1. 今日采集概览。
2. Demand 覆盖情况。
3. Ticket / Message 处理情况。
4. MIC 情报采集统计。
5. stock_data 采集统计。
6. 工具能力验证结果。
7. 结构化事件 Top N。
8. 10 分钟行情特征 Top N。
9. 数据质量问题。
10. 故障与降级。
11. 覆盖缺口。
12. 次日补采建议。
13. 成本、调用次数和预算使用。
14. 原始证据引用和 run id。

### 15.3 报告生成 CLI

```bash
agent-trade report collection-daily \
  --env prod \
  --trade-date 2026-06-11 \
  --format html \
  --output reports/collection_report_20260611.html
```

---

## 16. CLI 设计

### 16.1 Demand CLI

```bash
agent-trade demand validate --file demand.yaml --env test
agent-trade demand register --file demand.yaml --env test --activate
agent-trade demand list --env test --status active
agent-trade demand get --env test --demand-id demand_xxx
agent-trade demand suspend --env test --demand-id demand_xxx
agent-trade demand resume --env test --demand-id demand_xxx
agent-trade demand cancel --env test --demand-id demand_xxx
agent-trade demand compile --env test --demand-id demand_xxx --as-of 2026-06-11T10:30:00+08:00
```

### 16.2 Queue CLI

```bash
agent-trade queue publish --env test --topic collection.task --ticket-id ticket_xxx
agent-trade queue list --env test --topic collection.task --status ready
agent-trade queue inspect --env test --message-id msg_xxx
agent-trade queue retry --env test --message-id msg_xxx
agent-trade queue dead-letter list --env test
```

### 16.3 Runtime CLI

```bash
agent-trade runtime tick --env test --now 2026-06-11T10:30:00+08:00
agent-trade runtime recover --env test --agent intelligence_collector
agent-trade runtime heartbeat --env test --agent intelligence_collector
agent-trade runtime capability-validate --env test --tool stock_data_collector
```

### 16.4 Agent CLI

```bash
agent-trade agent run-once --env test --agent intelligence_collector
agent-trade agent run-until-idle --env test --agent intelligence_collector
agent-trade agent checkpoint --env test --agent intelligence_collector
agent-trade agent resume --env test --agent intelligence_collector --from-latest-checkpoint
```

### 16.5 Report CLI

```bash
agent-trade report collection-daily --env test --trade-date 2026-06-11 --format json
agent-trade report collection-daily --env test --trade-date 2026-06-11 --format html
```

---

## 17. 推荐包结构

```text
agent_trade/
  config/
    loader.py
    schemas.py
  demand/
    schemas.py
    registry.py
    compiler.py
    cli.py
  tickets/
    schemas.py
    repository.py
    cli.py
  queue/
    base.py
    sqlite_queue.py
    schemas.py
    cli.py
  runtime/
    controller.py
    heartbeat.py
    recovery.py
    capability_validation.py
    circuit_breaker.py
  intelligence_collector/
    agent.py
    task_intake.py
    target_normalizer.py
    cadence.py
    tool_executor.py
    mic_adapter.py
    stock_data_adapter.py
    quality_gate.py
    event_builder.py
    market_feature_builder.py
    report_builder.py
    checkpoint.py
  intelligence_query/
    service.py
    reader.py
    cli.py
  reports/
    html_renderer.py
    templates/
      collection_daily.html.j2
  storage/
    database.py
    migrations/
  tests/
    fixtures/
      demands/
      configs/
    scenarios/
```

---

## 18. MVP 落地顺序

### P0：基础设施闭环

1. SQLite schema 和 migrations。
2. Config YAML loader。
3. Demand Registry CLI。
4. SQLiteMessageQueue。
5. Ticket repository。
6. Runtime tick。
7. Agent run-once 消费 message。
8. Checkpoint 和 heartbeat。
9. HTML/JSON report skeleton。

### P1：真实工具接入

1. MIC adapter，真实调用。
2. stock_data adapter，真实 CLI 调用。
3. StockDataResponse 解析。
4. MIC report 解析。
5. 质量闸门。
6. 故障分级。
7. 采集结果落库。
8. HTML 日报填充真实数据。

### P2：能力验证和盘中 10 分钟

1. capability_validation 消息流程。
2. stock_data 5m/15m/realtime 能力验证。
3. tool_capabilities 表。
4. 10 分钟 bucket 调度。
5. 5m 聚合 10m / 15m 降级。
6. market_feature 表。
7. `market_feature.created` 消息。

### P3：多 Agent 消息化读取

1. `query.intelligence.request`。
2. `query.intelligence.response`。
3. Intelligence Query Service。
4. 分析员通过消息请求情报。
5. 风控通过消息请求质量和交易状态。

---

## 19. 最小真实 E2E 验收剧本

```bash
# 1. 初始化测试 SQLite
agent-trade db init --env test --reset

# 2. 校验配置
agent-trade config validate --env test --config-dir config/test

# 3. 注册 Demand
agent-trade demand register \
  --env test \
  --file tests/fixtures/demands/held_sellable_default.yaml \
  --activate

# 4. 盘前能力验证
agent-trade runtime capability-validate \
  --env test \
  --tool stock_data_collector

# 5. 编译盘中任务
agent-trade runtime tick \
  --env test \
  --now "2026-06-11T10:30:00+08:00"

# 6. 运行情报收集员
agent-trade agent run-until-idle \
  --env test \
  --agent intelligence_collector

# 7. 查看队列和 Ticket
agent-trade queue list --env test --status ready
agent-trade ticket list --env test --correlation-id corr_collection_20260611_000001

# 8. 生成 HTML 日报
agent-trade report collection-daily \
  --env test \
  --trade-date 2026-06-11 \
  --format html
```

验收标准：

| 项 | 预期 |
|---|---|
| Demand 注册 | active。 |
| Message Queue | demand.registered、collection.request、collection.task 正常投递和 ack。 |
| 能力验证 | `tool_capabilities` 有 stock_data 记录。 |
| 真实工具调用 | `collection_runs` 有 MIC / stock_data 真实 run。 |
| 质量闸门 | 成功、partial、失败均被正确分类。 |
| 结果落库 | 至少有 collection_result 或 data_quality_issue。 |
| HTML 日报 | 生成可读 HTML 文件。 |
| 重复运行 | 不重复创建相同 Demand、Ticket、Message、Feature。 |

---

## 20. 最终口径

情报收集员 Agent 第一版采用 SQLite 作为持久化底座，同时实现消息队列机制。Demand 是长期采集意图，Ticket 是业务任务和业务事实，Message 是可靠投递载体。Agent 不直接轮询业务表，而是消费消息队列中的采集任务。所有工具调用使用真实 `market_intelligence_collector` 和真实 `stock_data_collector`，测试环境通过小预算、测试 SQLite、测试 config 和测试输出目录隔离风险，不再以 mock 作为验收主路径。盘中默认最小调度粒度为 10 分钟，但所有频率、预算、阈值、降级和告警策略均放入 YAML 配置。stock_data 盘中能力必须先经过 capability validation，验证结果写入 `tool_capabilities` 后再决定使用 5m 聚合、15m 降级、交易状态检查或跳过。读取情报的 tool 仅用于内部测试、调试和 CLI；多 Agent 服务化时，情报查询通过消息机制完成。收盘后必须生成 JSON 结构化日报和 HTML 可读日报。整个 Agent 必须支持 heartbeat、lease、ack、retry、dead-letter、幂等、checkpoint、session rotation、circuit breaker 和崩溃恢复。
