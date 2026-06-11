# 情报收集员 Agent V0.5.1 架构说明

## 1. 代码边界

本 Agent 是 OpenClaw 中的一个独立业务 Agent。它不依赖 OpenClaw 的全局 memory / checkpoint 机制保存核心业务状态，而是使用三类 SQLite store：

| Store | 是否私有 | 内容 |
|---|---:|---|
| state DB | 是 | `agent_sessions`、`agent_checkpoints`、`agent_memories`、`runtime_heartbeats`、`circuit_breakers`、`tool_capabilities` |
| bus DB | 共享 | `messages`、`tickets`、`ticket_events` |
| data DB | 可共享 | `collection_demands`、`collection_tasks`、`collection_runs`、`structured_events`、`market_features`、`data_quality_issues`、`coverage_gaps`、`daily_collection_reports` |

这样 OpenClaw 同时运行多个 Agent 时，本 Agent 的 memory / checkpoint / session 不会污染其他 Agent，同时事件、Ticket 和查询结果可以通过共享 bus/data 被其他 Agent 消费。

## 2. OpenClaw 模型配置

配置文件：

```yaml
openclaw:
  model:
    primary: "openai/gpt-5.5"
    fallbacks: []
    require_registered: true
    allow_openclaw_default: false
```

关键规则：

1. 不允许默认继承 OpenClaw default model。
2. `primary` 必须是 OpenClaw 已注册模型引用。
3. `REPLACE_WITH_REGISTERED_OPENCLAW_MODEL`、`default`、`openclaw/default` 会被硬拦截。
4. 可以配置 fallbacks。
5. `intel-agent openclaw validate-model` 会用 `openclaw models list --plain` 尝试校验。

## 3. workspace_root 和路径

`runtime.workspace_root` 支持：

- 绝对路径；
- `auto`：读取 `OPENCLAW_WORKSPACE_ROOT` / `OPENCLAW_WORKSPACE` / `OPENCLAW_WORKDIR`；
- `openclaw`：尝试 OpenClaw CLI，再回退环境变量。

runtime 路径、报告路径和工具路径均相对 workspace root 解析，不依赖进程 CWD。

## 4. Message 与 Ticket 分离

Ticket 是业务记录，Message 是投递机制。

```text
Demand Registry
  ↓ compile
COLLECTION_REQUEST_TICKET
  ↓ publish message
SQLiteMessageQueue
  ↓ agent lease
COLLECTION_TASK_TICKET
  ↓ publish message
SQLiteMessageQueue
  ↓ agent execute
Tool Run / Event / Feature / Quality Ticket
```

## 5. 独立 Memory

`agent_memories.agent_id = config.agent.agent_id`。

情报收集员只能写入自己的经验记忆，例如：

- 哪些 query family 对某行业更有效；
- 哪些来源噪音较大；
- 哪些工具故障在何种条件下发生；
- 能力验证结果和降级经验。

事实性信息不应写入 lesson memory，而应写入公共数据池表，如 `structured_events` / `market_features` / `collection_runs`。

## 6. 独立 Checkpoint

`agent_checkpoints.agent_id = config.agent.agent_id`。

每次 `run_once` 后保存 checkpoint。崩溃恢复时应：

1. requeue 过期 lease；
2. 读取 latest checkpoint；
3. 读取 in_progress / open messages；
4. 按 idempotency_key 防重复；
5. 补 ack 已完成 Ticket 的消息；
6. dead-letter 生成 `FAULT_TICKET`；
7. 补发孤儿 Ticket 的 message。

## 7. 真实工具路径

主路径不使用 mock。

- MIC：通过 `from mic.api import AnalystAPI` 调用。
- stock_data_collector：通过 `python -m stock_data_ingestion.cli` 调用。

如果工具不可用，Agent 会生成失败结果 / `FAULT_TICKET` / `DATA_QUALITY_TICKET`，而不是伪造成功。

## 8. 10 分钟默认粒度

10 分钟是默认 Agent 调度粒度，配置在：

```yaml
cadence:
  intraday_bucket_minutes: 10
```

底层 stock_data_collector 不一定原生支持 10m。能力验证会测试 5m / 15m，并通过 `query bars` 确认实际分钟行存在。盘中特征会在 fetch 后再次 query 已落库行情，再按 10m bucket 聚合。

## 9. Retry 与 dead-letter

工具返回 retryable failure 时，消息不 ack，而是 nack 回 open；超过 `max_attempts` 进入 dead-letter。凭证、存储、质量冲突等人工处理问题会 ack 并生成质量/故障 Ticket，不盲目重试。
