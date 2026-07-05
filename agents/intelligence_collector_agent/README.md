# 情报收集员 Agent 代码包 V0.8

这是 Agent交易公司多 Agent A 股交易辅助系统中的 **情报收集员 Agent**。它面向 OpenClaw 多 Agent 运行环境设计，负责 Demand → Ticket → Message → 工具调用 → 质量闸门 → 事件/特征/日报的采集闭环。

本 Agent **不输出买卖建议、仓位、目标价或交易指令**。它只做采集、结构化、质量检查、消息投递、Checkpoint、Memory 和日报。

完整版本历史见 [ChangeLog.md](ChangeLog.md)。

## 0. V0.8 关键变更（A股 + 港股通可迭代研究池闭环，第四轮审阅采纳）

1. **事件→跟踪变量双层标签（schema v5）**：MIC 模型对每条事件输出 `tracking_variables`（变量/方向/强度/理由/置信度，只能从 target 声明清单中选），Agent 落库到新表 `event_variable_links`（confidence ≥ 0.65 记 accepted，否则 pending）；另有中文关键词规则产出 `keyword_candidate` 候选链接，一律 pending、不进 confirmed coverage。
2. **港股通结构化采集 `hk_connect_collector`**：AKShare（东方财富）拉取港股通资格 + 南向持股（量/市值/占比/1/5/10日变化），落新表 `hk_connect_snapshots`（每标的每日幂等一条）；daily 盘后为 `.HK` 目标自动追加 `hk_connect_daily_snapshot` 任务；akshare 为可选依赖（惰性导入，未安装只影响 HK 快照）。
3. **`theme_ids` 多主题归因**：公司保持单一 `industry_id` 主线，同时可挂多个跨主题（如出海制造）；三一/中车/潍柴/美的/海尔等已在 full YAML 打上 `industry_export_manufacturing`。
4. **`derived_from_demands` 运行时目标引用**：周/月/季复盘 Demand 不再注册时拷贝名单，而是每次规划时读取来源 daily Demand 的当前目标（增删自动跟随不漂移）；full YAML 三个复盘 Demand 已切换。
5. **评估 CLI `intel-agent eval`**：`eval coverage`（target × tracking_variable 覆盖矩阵）、`eval hk-connect`（港股通快照覆盖率与缺失清单）、`eval golden`（人工金标事件集 recall）；dashboard 新增今日变量映射与港股通快照统计。
6. **质量闸门变量覆盖规则**：目标有变量清单但本次零覆盖 → P2 降级留痕；低覆盖（缺失 ≥ 70%）→ 仅记录不降级。
7. **`all_events` 缓存复用补全（MIC 侧）**：cache/reuse 命中时克隆的事件明细现在也进入 `all_events`，全量事件落库契约在复用场景同样成立。

## 0.1 V0.7.3 关键变更（研究池维护闭环，第三轮审阅甄别采纳）

1. **planner 显式支持 `periodic_review`**：每 collect_mic 目标规划 1 个 MIC 深采任务，复盘 Demand 即使盘后也不追加 stock 任务（审阅所报"MIC 重复规划"经核对为误判，规划自 V0.7 起即为单次，测试持续守护）。
2. **每条主线默认跟踪变量**：batch spec 新增 `tracking_variables_by_industry` 段，公司条目按 `industry_id` 继承主线变量集（可单条目覆盖）；`research_pool_full.yaml` 已填入 8 条主线定制变量，175 家公司全部带变量。
3. **`copy_targets_from` + 周/月/季复盘并入 full YAML**：复盘 Demand 复用 daily 目标清单，无需重复名单；full YAML 内置周度行业景气复盘（周五）/ 月度公司研究卡（每月1日）/ 季度财报季复盘（1/4/7/10月15日）。
4. **全量事件落库**：MIC 输出新增 `all_events`，persister 与质量闸门优先使用；未进 Top5 展示的小事件（小额回购、库存边际变化、中标候选人公示）不再丢失。

## 0.2 V0.7.2 关键变更（研究效果结构化，第二轮审阅采纳）

1. **2026 年 A 股交易日历落地**：`market_calendar.holidays` 按沪深交易所 2026 年休市安排填入 19 个工作日休市日（春节/国庆等），`calendar validate --year 2026` 返回 ok；每年 12 月需追加次年条目。
2. **研究池目标元数据**：`request industry|company`（及 batch 条目）支持 `industry_id` 与 `tracking_variables`，存入 Demand target 供日报/分析员按主线与变量聚合；`examples/research_pool_full.yaml` 175 家公司已全部打上 `industry_id`，40 个港股代码统一补零为 5 位（入库 ticker 也归一为 `00700.HK` 形式）。
3. **周/月/季复盘 Demand**：Demand 支持 `cadence: weekly|monthly|quarterly` + `cadence_anchor`（weekly 默认周五），未到期的 runtime tick 直接跳过编译。示例：`examples/periodic_reviews.yaml`（周度行业景气复盘 / 月度公司研究卡 / 季度财报季复盘）。
4. **query_family 全链路（schema v4，老库自动迁移）**：MIC `top_events` 带 `source.query_family`，落库到 `structured_events.query_family`；dashboard 产出面板新增今日事件按 source_type / query_family 的统计。
5. **质量规则**：高置信度事件缺 `published_at` → P2 降级（`quality.mic.require_published_at_for_high_confidence`，阈值 0.75 可配）。

## 0.0.0 V0.7.1 关键变更（真实运行前加固）

采纳外部代码审阅意见，消除 full pool 真实启动的工程风险（详情与采纳/暂缓清单见 ChangeLog）：

1. **MIC 长任务防重复执行**：采集期间后台线程自动续租（`lease_heartbeat`）；新增 `tools.market_intelligence_collector.timeout_seconds`（默认 900s）硬超时，超时返回 retryable 的 `MIC_TIMEOUT`；消息重投时同一任务幂等键已有 success run 直接复用，不再花第二次预算。
2. **本地交易日统计**：dashboard 与日报的"今日/trade_date"统一按本地交易日换算 UTC 区间过滤（此前 Asia/Shanghai 凌晨数据会被算进前一天）；dashboard 显示 `today_local` / `today_utc`。
3. **MIC 档案写入原子化 + 文件锁**：`target_profiles.yaml` 临时文件 + `os.replace` 原子替换，`load->merge->save` 全程持 flock，并发 batch 不互相覆盖。
4. **batch 重跑配置显式化**：`request batch --update-demand-config` 才会把 `demands:` 覆盖应用到已存在的 Demand；不加参数时输出 skip warning（结果含 `demand_config_updated`），杜绝"改了 YAML 没生效"误判。
5. **交易日历校验**：新增 `intel-agent calendar validate [--year]`；`config validate` 输出日历检查，当年无节假日条目会明确告警。
6. **事件证据字段（schema v3，老库自动迁移）**：`structured_events` 新增 `source_url / source_domain / source_type / published_at / retrieved_at`；MIC `top_events` 输出带 `event_type / event_date / source{...}`；日报与 dashboard 展示来源类型。
7. **MIC 质量闸门强化**（`quality.mic.*` 可配置）：高优先级目标零事件、全部事件无来源 URL、全部事件仅弱权威来源（media/social/unknown）→ P2 降级并留痕。

## 0.0.1 V0.7 关键变更（研究池启动 + 一键申请采集）

按《A股_港股通_可迭代股票研究池_跟踪建议.md》落地"行业信息 + 目标公司信息"采集：

1. **一键申请采集工具 `intel-agent request`**（`request_center.py`）：一条命令申请对行业 / 公司 / 股票开始采集。
   - `request industry`：写入/合并 MIC `target_profiles.yaml` 行业档案，并把目标加入托管 Demand `demand_industry_research_daily`（不存在则自动创建为 active 的 daily_collection），发布 `demand.registered` 供 runtime tick 拾取。
   - `request company`：同上写公司档案 + 加入 `demand_company_research_daily`；A 股代码同时进 `pool_members`（默认 watchlist 层），港股代码自动 `collect_stock=false`（stock_data_collector 仅覆盖 A 股）。
   - `request stock`：仅股票数据（`collect_mic=false`），加入 `demand_stock_eod_daily`（盘后日线刷新）+ 股票池。
   - `request batch --file`：**一个 YAML/JSON 配置文件一次性注册整个研究池**（行业 + 公司 + 股票 + 每个 Demand 的预算/优先级覆盖）。MIC 档案单次写盘，每个托管 Demand 只升一个版本；重复执行幂等。完整示例：`examples/research_pool_full.yaml`（覆盖跟踪建议 md 的 8 条主线 + §3 核心名单约 108 家 + §4 上下游观察名单约 67 家，观察层走低预算 `demand_company_watch_daily`）。
   - `request remove / status`：从托管 Demand 移除目标；查看 MIC 已注册目标、托管 Demand 与股票池全景。
2. **目标级工具开关**：Demand target 支持 `collect_mic: false`（跳过 MIC 任务）与 `collect_stock: false`（跳过盘后行情刷新）；行业目标（无 ticker）不再在盘后误生成股票任务。
3. **MIC 注册一致性校验**：新增 `tools list-mic-targets`；`demand register` 时对将走 MIC 的 target_id 检查是否在 MIC `target_profiles.yaml` 注册，未注册的在输出中给出 `mic_unregistered_targets` warning（不阻断），避免执行期才报 `Unknown target_id`。
4. **研究池内容配置（MIC 侧，均为 YAML 无代码改动）**：
   - `target_profiles.yaml`：新增 8 条行业主线档案（AI算力/高端装备/电力系统/创新药/高股息央国企/平台互联网/资源周期/出海制造，含关注指标、上下游术语、代表公司）+ 首批 18 家核心公司档案。
   - `query_families.yaml` v0.4：新增 `early_signal`（涨价函/排产/中标候选人/盈利警告等早期信号词，跟踪建议 §6.10）与 `hk_connect`（港股通资格/南向持股/回购）两个查询族。
   - `source_packs.yaml` v0.4：新增 `industry_stats` 源包（统计局/能源局/工信部/行业协会），域名信源映射补充北交所、港交所、医保局、CDE、NMPA、行业协会等（跟踪建议 §5）。
5. **示例 Demand**（`examples/demands/`）：`industry_research_daily.json`（8 行业每日深采）、`company_research_daily.json`（18 家核心公司每日深采 + A 股盘后行情）、`market_black_swan.json`（行业级黑天鹅扫描，无需股票代码）。
6. **日报**：结构化事件 Top N 与覆盖缺口现在带 `target_id`，行业级（无 ticker）产出不再显示为空目标。
7. **仓库清理**：删除项目根目录误提交的运行产物 `mic.db`（MIC 默认 `sqlite:///mic.db` 相对进程 CWD，在项目根运行会重新生成），并加入 `.gitignore`；有意保留的数据库仍在 `data/` 下（已被忽略）。

本版验证：`PYTHONPATH=src pytest -q` 48 个离线测试全部通过（新增 13 个 request/batch/planner 测试）；MIC 侧配置经 MIC 自身测试套件（73 个）与 QueryPlanner 冒烟验证（行业/公司档案均能展开出中文查询计划）；CLI 冒烟覆盖 `request industry/company/stock/status` → `runtime tick` 消费 `demand.registered` 并编译采集请求 → `demand register` 未注册 target 告警全链路。

## 0.0 V0.6 关键变更（按设计说明书补齐功能）

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
  hk_connect_collector:   # V0.8：港股通结构化快照（可选依赖 akshare，未安装仅 HK 快照任务失败留痕）
    enabled: true
    provider: akshare
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

# 交易日历年度校验（V0.7.1）：当年无节假日条目会告警（config validate 也会输出该检查）
intel-agent --config config/intelligence_collector.yaml calendar validate --year 2026
```

### 一键申请采集（V0.7）

不写 JSON、不手改 MIC 配置，一条命令申请对某个行业 / 公司 / 股票开始采集：

```bash
CFG=config/intelligence_collector.yaml

# 申请采集一个行业主线（写 MIC 行业档案 + 加入 demand_industry_research_daily）
intel-agent --config $CFG request industry --name "AI算力" \
  --products "AI服务器,800G光模块,高多层PCB" \
  --companies "工业富联,中际旭创,沪电股份" \
  --metrics "AI服务器订单,合同负债,毛利率"

# 申请采集一家公司（写 MIC 公司档案 + 加入 demand_company_research_daily + A股进股票池）
intel-agent --config $CFG request company --name 北方华创 --ticker 002371.SZ \
  --products "刻蚀设备,薄膜沉积设备" --competitors "中微公司,拓荆科技"

# 港股公司：自动跳过 stock_data_collector（仅 MIC 采集）；ticker 自动补零归一为 00700.HK。
# V0.7.2：可附带研究主线与跟踪变量，供日报/分析员按主线与变量聚合。
intel-agent --config $CFG request company --name 腾讯控股 --ticker 0700.HK \
  --industry-id industry_internet_consumer \
  --tracking-variables "southbound_holding,buyback,revenue_growth,margin"

# 只要股票日线数据（不做 MIC 情报，盘后刷新，加入 demand_stock_eod_daily + 股票池）
intel-agent --config $CFG request stock --ticker 600519.SH --company-name 贵州茅台

# 一次性注册整个研究池（行业 + 核心公司 + 上下游观察名单），来自跟踪建议 md 的完整清单。
# 第一次建议加 --test-mode 小预算试跑，确认链路后不带 --test-mode 重跑一次即恢复正式预算（幂等）。
intel-agent --config $CFG request batch --file examples/research_pool_full.yaml --test-mode
intel-agent --config $CFG request batch --file examples/research_pool_full.yaml

# 重跑时如果修改了 YAML 中 demands: 段的预算/优先级/task_profile，默认不会应用到已存在的
# Demand（输出 warning 提示）；需要生效时显式加 --update-demand-config：
intel-agent --config $CFG request batch --file examples/research_pool_full.yaml --update-demand-config

# 周度/月度/季度复盘 Demand：demands: 段设 cadence: weekly|monthly|quarterly，runtime tick
# 只在到期日编译（weekly 默认周五，可用 cadence_anchor 调整）。V0.8 起 research_pool_full.yaml
# 内置的三个复盘 Demand 改用 derived_from_demands 运行时引用 daily 名单（daily 增删目标后
# 复盘自动跟随；copy_targets_from 注册时拷贝仍兼容）；examples/periodic_reviews.yaml 保留为最小演示。

# 研究效果评估（V0.8）：变量覆盖矩阵 / 港股通结构化覆盖 / 金标事件集 recall
intel-agent --config $CFG eval coverage --date 2026-07-06 --demand-id demand_company_research_daily
intel-agent --config $CFG eval coverage --date 2026-07-06 --include-candidates   # 纳入 pending 关键词候选
intel-agent --config $CFG eval hk-connect --date 2026-07-06
intel-agent --config $CFG eval golden --file examples/golden_events.yaml

# 观察层（上下游扩展名单）想暂停/恢复：
intel-agent --config $CFG demand suspend --demand-id demand_company_watch_daily
intel-agent --config $CFG demand resume  --demand-id demand_company_watch_daily

# 查看全景：MIC 已注册目标 / 托管 Demand 的目标清单 / 股票池
intel-agent --config $CFG request status

# 从托管 Demand 移除目标（MIC 档案保留，便于以后恢复）
intel-agent --config $CFG request remove --demand-id demand_company_research_daily --ticker 002371.SZ

# 列出 MIC 已注册的 target_id（demand register 时会自动校验并对未注册目标告警）
intel-agent --config $CFG tools list-mic-targets
```

说明：

- 托管 Demand（`demand_industry_research_daily` / `demand_company_research_daily` / `demand_stock_eod_daily`）首次申请时自动创建为 active；每次申请都会升 Demand 版本并发布 `demand.registered`，下一次 `runtime tick` 自动拾取，无需手工 compile。
- MIC 档案定位顺序：`tools.market_intelligence_collector.config_dir` > 已安装 `mic` 包所在目录 > 仓库内 `tools/market_intelligence_collector/config`。
- 重复申请同一目标是幂等更新（合并档案字段、替换 Demand 内目标），不会产生重复目标。
- 想控制预算先小规模试跑：加 `--test-mode`（预算被 `mic_task_defaults.test_mode_budget_profile` 钳制）。

### 实时监控看板

内置一个动态看板（前后端一体，标准库实现，无额外依赖）。后端每次请求都直接读三个 SQLite store 的当前状态，前端定时轮询 `/api/overview` 自动刷新，Agent / runtime 在其它进程持续写入时看板同步变化（WAL 允许并发读）：

```bash
# 已 pip install -e . 安装时：
intel-agent --config config/intelligence_collector.yaml dashboard --port 8700 --refresh-seconds 5

# 未安装、直接跑源码时：
PYTHONPATH=src python3 -m agent_trade_intel.cli --config config/intelligence_collector.yaml dashboard --port 8700 --refresh-seconds 5

# 然后浏览器打开 http://127.0.0.1:8700 （常驻进程，Ctrl+C 退出）
# 如需局域网其它机器访问，加 --host 0.0.0.0
```

看板内容：会话/心跳存活状态（active / recent / stale）、当前市场阶段、队列深度与死信、未关闭 Ticket、今日任务与工具调用流水、最新结构化事件与行情特征（异常分高亮）、open 状态的数据质量问题与覆盖缺口、Demand 列表、能力验证结果与推荐盘中模式、熔断器状态、最新 checkpoint 与日报。页面支持暂停/调整刷新间隔（2/5/10/30s），拉取失败会显示错误条并自动重试。接口：`GET /api/overview`（聚合 JSON）、`GET /healthz`。

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

### hk_connect_collector（V0.8）

需要满足：

- `pip install akshare`（可选依赖；数据来自东方财富，无需 API Key）。
- 未安装 akshare 时，HK 快照任务返回不可重试的 `AKSHARE_NOT_INSTALLED` 并留痕，MIC / A 股链路不受影响。
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

# 6. 注册 Demand（研究池启动：8 行业日采 + 核心公司深采 + 黑天鹅扫描）
intel-agent --config config/intelligence_collector.yaml demand register \
  --file examples/demands/industry_research_daily.json --activate
intel-agent --config config/intelligence_collector.yaml demand register \
  --file examples/demands/company_research_daily.json --activate
intel-agent --config config/intelligence_collector.yaml demand register \
  --file examples/demands/market_black_swan.json --activate
# （或用 intel-agent request industry/company/stock 逐个申请；盘中监测示例见 held_sellable_10m.json）

# 7. Runtime tick
intel-agent --config config/intelligence_collector.yaml runtime tick \
  --now "2026-06-11T10:30:00+08:00"

# 8. 运行 Agent
intel-agent --config config/intelligence_collector.yaml agent run-until-idle --max-messages 100

# 9. 查看状态
intel-agent --config config/intelligence_collector.yaml agent status

# 10. 生成日报
intel-agent --config config/intelligence_collector.yaml report daily --trade-date 2026-06-11

# 11. 启动实时监控看板（常驻进程，浏览器打开 http://127.0.0.1:8700，Ctrl+C 退出）
intel-agent --config config/intelligence_collector.yaml dashboard --port 8700 --refresh-seconds 5
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

