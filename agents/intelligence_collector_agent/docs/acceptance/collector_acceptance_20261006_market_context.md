# 情报收集员 Agent 验收：市场背景数据采集下沉到 stock_data_collector（2026-10-06）

依据：用户 2026-10-06 重构说明（8 节）。目标：Agent 不再直接调用 AKShare 取市场背景数据（A 股指数 / 港股指数 /
汇率 / 商品 / 利率），全部经 `stock_data_collector` 的 `fetch market-context` 取数；工具侧补齐能力与数据源兜底；
结果带实际数据日、单位口径、来源、质量与溯源；Agent 按质量信息决定是否计入当日覆盖。

运行环境：`/home/yu/.venv/mydev/bin/python`（Python 3.12，akshare 1.18.64），本机，`STOCK_DATA_PREFER_IPV4=true`
（已写入工具 `.env`；本机 IPv6 到国内站点黑洞，否则 requests 在 IPv6 上挂死直到超时）。

## 首屏判定

| 项 | 结论 |
| --- | --- |
| 架构边界 | **通过**。`agents/intelligence_collector_agent/src/agent_trade_intel` 下无 `akshare / tushare / baostock` import；`market_context_adapter.py` 仅剩 CLI 组装、JSON 解析、错误分类与结果映射。由 `tests/test_v09_market_context.py::test_agent_production_code_has_no_vendor_imports` 以源码 AST 扫描守护 |
| 工具复用 | **通过**。A 股指数复用 `index_data` / `IndexBarRecord` / `index_bars`；新增类别走同一 `IngestionRunner`（provider 顺序、raw 落盘、标准化、比较 / 合并、评分、SQLite + Parquet、幂等） |
| 全链路 | **通过**。Agent（真实 `IntelligenceCollectorAgent.run_once`）→ 子进程 CLI → `MarketContextService` → `AKShareAdapter` → 新浪 / 中行 / 中债，六个 context 全部 ticket `done`（§2） |
| 数据正确性 | **通过**（抽样对照 §2.3）。取值与源接口原始列一致，单位显式 |
| 新鲜度 / 历史语义 | **通过**。请求日 2026-10-06（国庆休市）→ A 股指数 / 商品 / 国债收益率如实返回 2026-09-30 并标 `stale`；HKDCNY / HSTECH 当日 fresh；历史区间请求不出现"今天"的值（§1.7） |
| 质量与溯源 | **通过**。每条结果带 `provider / source_api / source_url / staleness_days / warnings / anomalies / record_ids / ingestion_run_ids / raw_payload_ids` |
| 幂等 | **通过**。同一请求重复执行从库回放（`served_from_store`），耗时 ~5s，数值一致；realtime 以分钟为幂等粒度 |
| 失败处理 | **通过**（离线测试 + 真实东财失败样本）。东财被 WAF 拒绝自动回退新浪并在 `warnings` 留 `source_fallback:`；未知 symbol 不重试并提示修 `market_context_sources.yaml`；超时可重试 |
| 回归 | **通过**。Agent 239 passed（含既有 MIC / 股票 / 港股通 / 日报 / 看板），工具 145 passed（126 既有 + 19 新增） |
| 未验证 | 东财 `stock_zh_index_daily_em`、`stock_hk_index_daily_em`（本机被 WAF `RemoteDisconnected`，未配置 `EASTMONEY_COOKIE`）；`bond_zh_us_rate` 兜底、`futures_zh_spot` 盘中时段快照（验收时为休市，仅验证了休市快照归属日校正）；BaoStock 对 `index_data` 的第二源交叉校验 |
| 评审四项修改（同日晚） | 见 **§6**：快照日期入库前确认 / 业务参数只校验 / 同日更新 + 历史表 / 隔离数据，逐项区分"模拟数据回归通过"与"真实供应商采集通过"。回归：工具 179 passed，Agent 242 passed |
| 评审补齐两项 + 日志 + 七类本地验证（10-07 凌晨） | 见 **§7**：请求—记录关联表、隔离日线阻断快照日期确认、JSONL 结构化日志；七类场景（3 类真实采集 `data_mode=live`、4 类模拟 `data_mode=simulated`）全部通过，材料在 `logs/acceptance/market_context_review_20261006/`。回归：工具 187 passed，Agent 245 passed |

## 1. 工具侧真实样本（CLI 直接调用，2026-10-06）

命令形如 `python -m stock_data_ingestion.cli --config-dir config fetch market-context --context-type <T> --symbol <S> --as-of 2026-10-06 --compact`。

| # | 类别 / 代码 | 命中接口 | `data_date` | 取值 | 单位 | 新鲜度 | 变动口径 |
|---|---|---|---|---|---|---|---|
| 1.1 | `fx` HKDCNY（`spot_sell`） | `currency_boc_sina`（中行牌价） | 2026-10-06 | 85.59 | CNY per 100 HKD | fresh, 0d | percent, period_unit=observation |
| 1.2 | `hk_index` HSTECH | `stock_hk_index_daily_sina` | 2026-10-05 | 4183.68018 | index_points | fresh, 1d | percent, trading_day |
| 1.3 | `commodity` CU0 futures 1d | `futures_zh_daily_sina` | 2026-09-30 | close 109680 / settle 109570 | CNY/ton | stale, 6d（tolerance 3d）→ `partial_success` | percent, trading_day |
| 1.4 | `commodity` CU0 realtime | `futures_zh_spot` | 2026-09-30（由同合约日线在入库前确认，`date_confidence=confirmed_last_session`，warning `snapshot_belongs_to_previous_session`；评审后口径见 §6.1） | latest 109680 | CNY/ton | stale | `snapshot_vs_pre_settle` +0.3477%；1/5/20p 来自已确认日线 |
| 1.5 | `interest_rate` CN_CGB_10Y | `bond_china_yield`（中债曲线 10Y 列） | 2026-09-30 | 1.6822 | percent | stale, 6d | percentage_point + basis_points |
| 1.6 | `interest_rate` SHIBOR_3M | `rate_interbank`（指标"3月"） | 2026-09-30 | 1.43 | percent | stale | percentage_point |
| 1.7 | `equity_index` 000300 | `stock_zh_index_daily`（新浪，东财 `stock_zh_index_daily_em` 失败后回退） | 2026-09-30 | 4357.616 | index_points | stale, 6d | percent, trading_day |
| 1.8 | `hk_index` HSTECH 历史 `--start-date 2026-09-01 --end-date 2026-09-15` | `stock_hk_index_daily_sina` | 最后一条 2026-09-15 | 11 个观测 | index_points | 历史模式不判新鲜度 | 20p → null, `reason=insufficient_history`（窗口内样本不足，不补零） |

落库：`fx_rates` / `index_bars` / `commodity_prices` / `interest_rates`，另有 `raw_payload_index`、`source_fetch_logs`、
`ingestion_runs`、Parquet 分区（`data_type=fx_rate|commodity_price|interest_rate|index_bars`）。
重复执行 1.1–1.7：`warnings` 含 `served_from_store`，不再访问外网。

## 2. Agent 全链路真实运行（2026-10-06 21:01 本机）

驱动：临时脚本构造 `IntelligenceCollectorAgent`（临时三库，`tools.stock_config_dir=tools/stock_data_collector/config`，
`stock_working_dir=tools/stock_data_collector`，`python_executable=/home/yu/.venv/mydev/bin/python`），
读取 `examples/research_pool_full.yaml` 的 `market_contexts` 经 `RequestCenter._market_context_entry` 生成目标
（0 条迁移 warning——示例 YAML 已无供应商字段），逐条创建 `COLLECTION_TASK_TICKET` + `intelligence.collection`
消息并 `run_once`。

### 2.1 Ticket 与快照

| context_id | 类型 / 代码 | ticket | `data_date` | 取值 | 新鲜度 | 1p / 5p / 20p | 口径 | 命中接口 | records |
|---|---|---|---|---|---|---|---|---|---|
| index_csi_300 | equity_index 000300 | done | 2026-09-30 | 4357.616 index_points | stale 6d | +0.2855 / −4.1142 / −5.5042 | percent | `stock_zh_index_daily`（`source_fallback: stock_zh_index_daily_em failed: RemoteDisconnected`） | 38 |
| index_hang_seng_tech | hk_index HSTECH | done | 2026-10-05 | 4183.68018 index_points | fresh 1d | +0.6191 / −2.9709 / −8.4494 | percent | `stock_hk_index_daily_sina` | 41 |
| fx_hkd_cny | fx HKDCNY spot_sell | done | 2026-10-06 | 85.59 CNY per 100 HKD | fresh 0d | −0.0817 / −0.0817 / −0.1517 | percent (observation) | `currency_boc_sina` | 246 |
| commodity_copper | commodity CU0 futures | done | 2026-09-30 | 109680 CNY/ton (close) | stale 6d | +0.1004 / −1.4732 / +0.4212 | percent | `futures_zh_daily_sina` | 38 |
| commodity_lithium_carbonate | commodity LC0 futures | done | 2026-09-30 | 118420 CNY/ton (close) | stale 6d | −0.3031 / −11.8899 / −24.9842 | percent | `futures_zh_daily_sina` | 38 |
| rate_cn_cgb_10y | interest_rate CN_CGB_10Y 10Y | done | 2026-09-30 | 1.6822 percent | stale 6d | +0.0152 / +0.0031 / −0.0039 | percentage_point | `bond_china_yield` | 39 |

每行 `market_context_snapshots` 均填充 `data_date / is_fresh / freshness_status / metric / change_kind / provider /
source_api / tool_request_id / quality_json / provenance_json`（`provenance.ingestion_run_ids` 例：
`run_20261006_210137_50110bf3`）。

### 2.2 质量问题与覆盖

- `data_quality_issues`：4 条 P3 `market_context_stale`（index_csi_300 / commodity_copper /
  commodity_lithium_carbonate / rate_cn_cgb_10y），文案"最新数据日期 2026-09-30，距请求日 6 天，超出容忍 3 天；
  已保存但不计入当日覆盖"。无 P0–P2。
- `CoverageEvaluator.market_context_coverage(trade_date=2026-10-06)`：`contexts_with_snapshot=6`，
  `contexts_fresh=2`，`stale_contexts=[commodity_copper, commodity_lithium_carbonate, index_csi_300, rate_cn_cgb_10y]`，
  `unknown_freshness=[]`。
- circuit breaker 未触发（stale 为成功结果；东财失败在工具内兜底，不上抛）。

### 2.3 与源数据对照（抽样）

| 样本 | 源接口原始值 | 工具结果 | 一致 |
|---|---|---|---|
| HKDCNY 2026-10-06 | `currency_boc_sina(symbol=港币)` 最后一行 `中行钞卖价/汇卖价=85.59` | `spot_sell=85.59`，`quote_basis=100` | 是 |
| HSTECH 2026-10-05 | `stock_hk_index_daily_sina(symbol=HSTECH)` 最后一行 `close=4183.68018` | 4183.68018 | 是 |
| CU0 2026-09-30 | `futures_zh_daily_sina(symbol=CU0)` 最后一行 `close=109680, settle=109570, hold→open_interest` | close 109680 / settle 109570 | 是 |
| CN_CGB_10Y 2026-09-30 | `bond_china_yield` 曲线"中债国债收益率曲线" `10年=1.6822` | 1.6822 percent | 是 |
| 000300 2026-09-30 | `stock_zh_index_daily(symbol=sh000300)` 最后一行 `close=4357.616` | 4357.616 | 是 |

### 2.4 幂等重放

同一脚本第二次运行（新的临时 Agent 三库、同一工具库）：六个 ticket 仍 `done`，总耗时 ~5s，
六个取值 / `data_date` / 变动值与首次完全一致，工具 `warnings` 含 `served_from_store`。

## 3. 离线测试

- 工具：`tools/stock_data_collector/tests/test_market_context.py` 19 项（配置解析与 symbol 注册表、五种 layout 的
  适配器行解析、兜底与错误分类、休市快照归属日、fresh / stale / 历史 / 百分点 + bp / A 股指数新浪兜底 / 幂等回放 /
  缺失 / 未知代码、请求校验、collector + CLI fetch 与 query）。全套 145 passed。
- Agent：`tests/test_v09_market_context.py` 14 项 + `tests/test_v081_reviewer_closure.py` 更新（边界源码扫描、
  假 CLI 适配器 fresh / stale / 类别参数 / 旧字段 / 失败分类、持久化列、schema v10 迁移、覆盖 fresh / stale / unknown、
  agent 任务 fresh 计覆盖 / stale 留痕不触发 breaker / 可重试重排队 / 不可重试人工处理、request center 拒绝无身份目标与
  供应商字段 warning）。全套 239 passed。

## 4. 配置迁移

- `examples/research_pool_full.yaml` `market_contexts` 六条已改为身份 + 需求字段（`context_type / symbol / metrics /
  instrument_type / tenor / max_staleness_days`），头部注释说明字段归属。
- 供应商绑定集中在 `tools/stock_data_collector/config/market_context_sources.yaml`（`schema_version:
  market_context_sources.v1`）：函数名、参数模板、列映射、单位、兜底顺序、业务代码注册表。
- 旧 YAML 仍含 `akshare_func / akshare_args / date_column / value_column / unit / provider / source_url` →
  `demand register` 输出迁移 warning 并忽略（测试 `test_v09_market_context.py::test_request_center_rejects_targets_without_business_identity`、
  `test_v081_reviewer_closure.py` 的 request batch 用例）。
- `config/intelligence_collector.yaml` `tools.market_context_collector` 去掉 `provider`，保留 `enabled /
  timeout_seconds`。
- 历史 `market_context_snapshots` 行保留，新列 NULL → 看板 / `eval` 显示"新鲜度未知（旧数据）"。

## 5. 已知限制与建议

1. **新鲜度按日历日**：`staleness_days` 为日历日差，国庆长假期间 A 股指数 / 商品 / 国债收益率的 9/30 数据会被标
   stale 并不计入覆盖。这是如实反映，但若希望"休市期间最近一个交易日算 fresh"，可由 Agent 侧按 `market_calendar`
   把 `max_staleness_days` 动态放大到"上一交易日距今天数 + 容忍"，再传给工具；本轮未改。
2. 东财接口在本机不可达，A 股指数当前只有新浪单源，`single_source` warning 常驻；配置 `EASTMONEY_COOKIE` 后可恢复双源。
3. 盘中 `futures_zh_spot` 实时快照（供应商只给时分秒、与上一交易日日线不匹配）按评审规则必须返回 `unknown_date`
   （§6.1），本轮只有模拟数据回归覆盖；交易时段的真实供应商采集未验证（验收日休市）。

## 6. 评审四项修改验收（2026-10-06 23:18–23:24 本机）

依据：用户 2026-10-06 评审 comments（商品快照日期 / 业务参数 / 同日更新 / 隔离数据）。每一项分别标注
**模拟数据回归通过**（`tests/test_market_context.py`、`tests/test_v09_market_context.py`，假 AKShare / 假 CLI）与
**真实供应商采集通过**（`python -m stock_data_ingestion.cli fetch market-context` 直连新浪 / 中行 / 中债，
`STOCK_DATA_PREFER_IPV4=true`，工具库 `tools/stock_data_collector/data/stock_data.db`）。

### 6.1 商品快照日期：入库前确认一次，重复读取不再推断

| 验收项 | 模拟数据回归 | 真实供应商采集 |
|---|---|---|
| 首次请求 CU0 realtime：`data_date=2026-09-30`，`is_fresh=false` | **通过** `test_snapshot_date_first_request_confirms_previous_session_before_storage`：库中 `commodity_prices` realtime 行 `trade_date=2026-09-30`、`observed_at=2026-09-30 15:00`、`date_confidence=confirmed_last_session`，`date_resolution_details` 含 `vendor_time_value=150000 / observed_date_inferred_from_fetch=true / confirmation_bar_record_ids×2 / confirmation_reason`；日线 30 根由同一 runner 日线链落库 | **通过**（23:23:47 新进程、新分钟）：`partial_success`，`data_date=2026-09-30`，`observed_at=2026-09-30T15:00:00+08:00`，`value=109680`，`is_fresh=false`，`status=stale`（6d），warning `snapshot_belongs_to_previous_session`；库行 `rec_0e54cdfb…` `date_confidence=confirmed_last_session` |
| 同一分钟重复请求：仍 2026-09-30 / false | **通过** `test_snapshot_date_repeat_same_minute_and_after_restart_reuse_confirmed_record`：0 次供应商调用，`_confirm_snapshot_dates` 0 次调用，`served_from_store` | **通过**（23:23:48，新进程）：`served_from_store`，`data_date / observed_at / value / is_fresh` 与首次一致 |
| 进程重启后重复请求：仍 2026-09-30 / false | **通过**（同上，新 `IngestionRunner` + 新 `Database` 同一 sqlite 文件） | **通过**（23:23:48 第三个进程）：结果同上 |
| 三者的序列日期 / `observed_at` / 变化端点完全一致 | **通过**：`series[].data_date`、`series[].observed_at`（含时区）、`changes{snapshot_vs_pre_settle,1p,5p,20p}.(from_date,to_date,value)`、`record_ids` 三次相同；`1p` from 2026-09-29 to 2026-09-30 | **通过**：三次均 `series=[(2026-09-30, 2026-09-30T15:00:00+08:00)]`，`1p=+0.100392%（09-29→09-30）`、`5p=−1.47323%（09-22→09-30）`、`20p=+0.421168%（09-01→09-30）`、`snapshot_vs_pre_settle=+0.347667%`，`record_ids=[rec_0e54cdfb49374716996684b143e6c459]`、`raw_payload_ids` 相同 |
| 日线获取失败且供应商只有时分秒 → 日期未知，不能返回当天新鲜 | **通过** `test_snapshot_date_unknown_when_daily_chain_fails_never_today_fresh[holiday/live]`：`status=failed`，`data_date=null`，`observed_at=null`，`usable=false`，`is_fresh=null`，`quality.status=unknown_date`，`SNAPSHOT_DATE_UNCONFIRMED retryable=true`，`commodity_prices` 0 行，`raw_payload_index` ≥1 | 未人为制造日线失败；但 23:19:53 的一次真实请求因旧库日线重复行（见 6.5）匹配失败，返回的正是 `failed / unknown_date / data_date=null / is_fresh=null / SNAPSHOT_DATE_UNCONFIRMED`，未写标准记录、raw 保留——**真实路径行为符合规则** |
| 盘中快照（价格已偏离上一日线收盘）→ 未知，不按采集机时间归属当天 | **通过** `test_snapshot_date_live_session_time_only_is_unknown_even_with_daily_bars` | 未验证（验收日休市） |
| 供应商给完整日期 → 直接使用，不走确认 | **通过** `test_snapshot_date_vendor_full_timestamp_is_kept_without_confirmation`（`date_confidence=vendor_timestamp`，无 `snapshot_belongs_to_previous_session`） | 未验证（新浪 `futures_zh_spot` 只给 `time=HHMMSS`） |
| 旧记录没有确认依据（`date_confidence` NULL）→ 按未知处理，不按 `vendor_timestamp` | **通过** `test_snapshot_date_legacy_record_without_confidence_is_unknown_not_vendor_timestamp`（库回放 → `unknown_date`） | 旧库中 20:27 写入的 realtime 行 `trade_date=2026-10-06, date_confidence=NULL` 仍在当前表，但业务键不同、未被任何成功 run 关联，不会被回放；按规则若被读取即为未知 |
| 旧 SQLite 补列 | **通过** `test_snapshot_date_columns_are_added_to_legacy_sqlite`（`ensure_columns` 补 `date_confidence / date_resolution_details`，幂等） | **通过**：真实库 `pragma table_info(commodity_prices)` 含两列（由 `Database.init()` 自动补齐） |

### 6.2 业务参数：symbol 决定对象，其余只做一致性校验

| 请求 | 模拟数据回归 | 真实供应商采集 |
|---|---|---|
| CU0，不带其他参数 → 正常 | **通过** `test_business_params_consistent_with_symbol_are_accepted` | **通过**（§6.1 全部 CU0 请求） |
| CU0 + `contract=CU0` + `instrument_type=futures` → 正常 | **通过** | **通过**：`partial_success`，`identity={commodity: copper, instrument_type: futures, market: SHFE, contract: CU0, …}`，`stock_data_response.status=success` |
| CU0 + `contract=CU2612` → `INVALID_REQUEST`，0 次供应商调用 | **通过** `test_business_params_conflicting_with_symbol_fail_without_vendor_call`：`fake_ak.calls == []`，五张表 0 行，`retryable=false`；消息与评审样例逐字一致（`test_business_params_example_message_matches_review_wording`） | **通过**：`failed`，`INVALID_REQUEST retryable=false`，`symbol=CU0 的 contract 配置为 CU0，请求 contract=CU2612，与 symbol 绑定不一致。请使用已注册的 CU2612 symbol。`，`stock_data_response.request_id=null`（未进入 runner） |
| CU0 + `instrument_type=spot` → `INVALID_REQUEST`，0 次调用 | **通过** | **通过**：`symbol=CU0 的 instrument_type 配置为 futures，请求 instrument_type=spot，与 symbol 绑定不一致。…` |
| US_CGB_10Y + `tenor=2Y` → `INVALID_REQUEST`，0 次调用 | **通过** | **通过**：`symbol=US_CGB_10Y 的 tenor 配置为 10Y，请求 tenor=2Y，与 symbol 绑定不一致。…` |
| 其他字段（`market` 对 commodity / fx / hk_index，`rate_type` 对 interest_rate） | **通过**（同一参数化用例） | 未逐一执行 |
| 身份与供应商参数全部来自配置 | **通过** `test_identity_and_vendor_args_come_from_config_not_request`（`_market_context_identity` 忽略请求覆盖；`futures_zh_daily_sina(symbol=CU0)`） | **通过**（上表 identity 来自 yaml） |

### 6.3 同日更新：当前表最新一条，历史表保存被替换的完整记录

| 验收项 | 模拟数据回归 | 真实供应商采集 |
|---|---|---|
| 9 点 85.59 → 当前 85.59 | **通过** `test_same_day_update_replaces_current_and_archives_full_old_record` | **模拟数据专项**：中行牌价当日不会在验收窗口内变化，无法真实复现 85.59→86.59 |
| 10 点 86.59 → 当前 86.59；历史表保留 85.59 及原 request_id / raw_payload_id / field_provenance | **通过**：`record_id / request_id / ingestion_run_id / raw_payload_id / raw_payload_ref / raw_hash / fetch_time` 整体替换，新 `field_provenance.rate.raw_payload_id == 新 raw_payload_id`；`market_context_record_revisions` 一行 `record_id=旧 id, superseded_by_record_id=新 id, record_json.rate=85.59, record_json.raw_payload_id=旧 raw`；`get_raw_ref_by_record_id(旧 id)` 返回旧 raw 引用 + `archived=true` | 真实库中替换机制已发生：23:23 的 CU0 realtime 重采替换了 23:18 的记录（`market_context_record_revisions` 含 1 行 realtime 归档），23:18 的日线重采归档了 20:27 的 38 根日线 |
| 10 点重复 / 进程重启后重复 → 86.59，record_id / raw_payload_id 为 86.59 那次采集的 | **通过** `test_same_day_update_repeat_and_restart_return_latest_collection`（`served_from_store`，`record_ids` 与 10 点一致，序列中无 85.59） | **通过**（§6.1 的 23:23:48 两次回放返回 23:23:47 采集的 record_id / raw_payload_id） |
| 迟到的旧结果不得覆盖 | **通过** `test_same_day_update_late_arriving_older_result_does_not_overwrite`（`fetch_time=08:00` 的 85.59 到达 → 当前仍 86.59、record_id 不变、无新归档、warning `stale_update_rejected`）；`test_repository_upsert_rules_direct`（更旧 / 同龄保留现有，更新替换，`provider_update_time` 优先于 `fetch_time`） | 未验证（需人为制造迟到结果） |
| 旧库同键重复行收敛 | **通过** `test_legacy_duplicate_rows_collapse_into_one_current_record`（两行 → 全部归档、只留一条、快照确认仍成功） | 真实库 CU0 日线仍有 38 日 × 2 行（20:27 / 21:01 两次旧代码写入；23:18 只替换了其中一组），确认步骤已按日期去重取最新一行；下一个小时的日线重采会把剩余重复行归档 |

### 6.4 隔离数据（PR #1 `91103dc`）

| 验收项 | 模拟数据回归 | 真实供应商采集 |
|---|---|---|
| `quarantined / manual_review_required / conflicted_high / failed` → `status=failed`、`quality.status=failed`、`usable=false`，保留值 / 来源 / 拦截原因，`is_fresh` 独立 | **通过** `test_service_blocks_upstream_validation_on_fetch_and_store_replay`（首采 + 库回放） | 不适用（真实采集无被隔离记录；拦截由 `_rescore_record` 注入模拟） |
| 多记录聚合保留拦截状态 | **通过** `test_service_fx_aggregation_preserves_blocking_status` | 同上 |
| 被拦截的变化端点 → change null + reason | **通过** `test_service_does_not_compute_change_from_quarantined_reference` | 同上 |
| Agent 不存为有效快照、不计覆盖 | **通过** `test_agent_upstream_quarantined_value_does_not_count_as_coverage` | 同上 |

### 6.5 Agent 侧映射

| 验收项 | 模拟数据回归（假 CLI） | 真实链路 |
|---|---|---|
| `unknown_date` + `SNAPSHOT_DATE_UNCONFIRMED`（可重试）→ ticket `open` 重排队，`market_context_snapshots` 0 行，覆盖 0，P2 `market_context_collect_failed`；适配器 `quality.status=unknown_date`、`is_fresh=None`、`data_date / observed_at / value=None` | **通过** `test_agent_unknown_snapshot_date_is_not_saved_and_is_retried` | 未单独跑 Agent 链路（工具 CLI 真实输出格式即 6.1 所示 `failed / unknown_date`） |
| `INVALID_REQUEST`（参数不一致）→ ticket `failed`，人工处理，不存快照 | **通过** `test_agent_business_param_mismatch_is_manual_not_retried` | 同上 |

### 6.6 回归与遗留

- 工具 `tests/test_market_context.py` 53 项（原 25 + 新增 28），全套 **179 passed**；Agent `tests/test_v09_market_context.py`
  17 项，全套 **242 passed**（`/home/yu/.venv/mydev/bin/python -m pytest -q tests`）。
- 真实库遗留：20:27 旧代码写入的 realtime 行 `rec_eafa5a15…`（`trade_date=2026-10-06`，`date_confidence=NULL`，按新规则属"日期未知"）
  与 21:01 的 38 根重复日线仍在当前表，未在本轮删除；不影响新请求（不被成功 run 关联、确认步骤已去重）。
- 未验证：盘中真实快照（休市）、供应商带完整日期的快照、真实迟到结果、85.59→86.59 的真实当日变价。

## 7. 评审补齐两项 + 结构化日志 + 七类本地验证（2026-10-07 00:14–00:23 本机）

代码 commit：`4d10665`（= `5eb4c3d` 主改动 + 两处小修：日期未知 WARNING 只发一次、Agent 日志 `data_mode` 默认字段；`market_context_review_20261006/*/inputs.json` 的 `code_commit` 均指向它）。
运行者：本机 `/home/yu/.venv/mydev/bin/python`；真实场景走真实 CLI 子进程 + 真实供应商 + 工具自己的 `data/stock_data.db`；
模拟场景复用 `tests/test_market_context.py` 的 `FakeAK` 与时钟控制（`market_context_requests.now_asia_shanghai` 钉死），
每个场景独立临时 SQLite（`/tmp/mctx_verify/work/<scenario>/db.sqlite`）。日志事件字段带 `data_mode=live|simulated`。

### 7.1 补齐项

| 项 | 实现 | 测试（模拟数据回归） |
| --- | --- | --- |
| 请求—记录关联 | 新表 `market_context_request_records(request_id, record_type, record_id)` 联合主键；`_persist()` 对本次请求最终采用的每条记录（`inserted / replaced / kept_existing`）写关联，与记录更新同一事务；`QueryService.resolve_request_records()` 先查关联表，关联记录已归档则沿 `superseded_by_record_id` 替换链到当前记录，无关联才回退旧的按记录 `request_id` 查询；记录自身 `request_id` 不改 | `test_same_day_update_late_arriving_older_result_does_not_overwrite` 追加：B 重复请求、重启后 B 请求均 86.59 且溯源为 A 采集的记录；`test_request_record_links_cover_insert_replace_and_keep` |
| 隔离日线阻断日期确认 | 阻断集合 `schemas/quality.py: BLOCKING_VALIDATION_STATUSES = {quarantined, manual_review_required, conflicted_high, failed}`，日期确认（`IngestionRunner._match_snapshot_to_last_session`）与汇总层（`MarketContextService._BLOCKING_VALIDATION_STATUSES`）同一引用；先按日期去重，取最近两个交易日，任一条阻断 → 立即 `SNAPSHOT_DATE_UNCONFIRMED`，不跳到更早日期；错误消息与 `date_resolution_details.reference_bars` 含参考记录 ID 与状态 | `test_snapshot_date_blocked_reference_bar_fails_confirmation[last]` / `[previous]`、`test_blocking_statuses_are_shared_between_confirmation_and_summary` |
| 结构化日志 | 工具 `logging_config.py`（JSONL、固定关联字段、`log_context` ContextVar、`--debug/--log-file/--trace-id`、`main()` 单次初始化、20 MiB×5）；Agent `logging_setup.py`（`agent.jsonl`、`ToolLogSink` → `tool_stderr.jsonl`、`--debug` 转发、`agent_outcome`）。事件清单见工具 README §6.17 | 工具 `test_logging_events_carry_correlation_fields_and_trace`、`test_logging_warnings_for_rejection_unknown_date_and_block`、`test_logging_info_level_has_no_per_record_detail`、`test_cli_debug_and_log_file_flags`；Agent `test_adapter_forwards_debug_and_trace_and_sinks_stderr_on_success_failure_and_timeout`、`test_agent_emits_agent_outcome_and_passes_ticket_trace`、`test_agent_cli_debug_flag_enables_debug_and_tool_forwarding` |

### 7.2 七类场景材料索引

材料位置：`agents/intelligence_collector_agent/logs/acceptance/market_context_review_20261006/<scenario>/`
（运行产物放 `logs/`，`logs/` 已在 `.gitignore`，本地保留、不入库；审查时按下表目录名在本机打开）。
每个目录：`inputs.json`（输入参数、commit、每次调用的摘要、`checks` 核对表）、`response_N.json`（每次调用完整响应）、
`debug.jsonl`（工具 DEBUG 日志；07 场景为 Agent 转存的工具 stderr）、`agent.jsonl`（涉及 Agent 时）、`db_export.json`
（仅该场景的当前记录、归档记录、请求—记录关联、快照日期确认依据）。驱动脚本在同目录 `_drivers/`（可重跑；输出目录即本 `logs/acceptance/` 路径）。

| 场景 | 模式 | 目录 | 调用 | 结果 | 核对 |
| --- | --- | --- | --- | --- | --- |
| 五类真实采集 | **live** | `01_live_five_categories` | 000300 / HSTECH / HKDCNY / CU0 1d / CN_CGB_10Y 各 1 次 | 000300 `4357.616 index_points @2026-09-30 stale`；HSTECH `4223.08008 @2026-10-06 fresh`；HKDCNY `85.59 CNY per 100 HKD @2026-10-06 fresh`；CU0 `109680.0 CNY/ton @2026-09-30 stale`；CN_CGB_10Y `1.6822 percent @2026-09-30 stale`。`source_crosscheck.json` 为 CLI 调用后对同一 akshare 函数的直接读取，五项日期与数值逐一相等；请求日 2026-10-07（跨午夜运行），非当日数据均带 `data_date_matches_as_of=false / staleness_days`，超出容忍的标 `stale` | 23/23 |
| 实时快照重复读取 | **live** | `02_live_realtime_reread` | CU0 realtime 三个新进程，00:22:00 / :01 / :02 | 幂等键三次相同（`…:realtime:::akshare:latest:202610070022`）；同记录 `rec_924985f4…`、同日期 2026-09-30、同值 109680.0、同来源；第 1 次 `snapshot_date_resolution` 1 条（`confirmed_last_session`，参考日线 `rec_8b799004…`@09-30 与 `rec_3598cf07…`@09-29），第 2/3 次只有 `store_replay`，无 `snapshot_date_resolution`、无 `provider_call`。`db_export.json.snapshot_date_confirmation` 含 realtime 记录的 `date_resolution_details` 与两条参考日线 | 9/9 |
| 参数冲突 | **live** | `03_live_param_conflict` | CU0 1d 正常 → `--contract CU2612`；US_CGB_10Y 正常 → `--tenor 2Y` | 两次冲突均 `failed / INVALID_REQUEST / retryable=false`，消息与规格原文一致；对应 trace 的日志里 `provider_call` 为 0、无 `record_write`，`identity_check decision=rejected` 列出冲突字段，`business_param_rejected` WARNING 各 1 条 | 12/12 |
| 同日更新 | simulated | `04_same_day_update` | 09:00 85.59 → 10:00 86.59 → 同幂等键重复 → 重启后重复 | 当前行 86.59、新 `record_id / request_id / raw_payload_id`；旧记录整条进 `market_context_record_revisions`（`superseded_by_record_id`=新 id）；重复与重启 0 次供应商调用、`served_from_store`、溯源为新记录 | 9/9 |
| 迟到旧结果 | simulated | `05_late_older_result` | 10:00 A=86.59；11:00 B 的抓取时间钉为 08:00（旧）→ 被拒；11:30 B 重复；重启后 B | B 三次均 86.59；当前行不变（`request_id` 仍为 A）、无新归档；`market_context_request_records` 有 (B, fx_rate, A 的记录) 关联；`store_replay` 事件 `resolution_source=request_record_links`，`QueryService.resolve_request_records` 命中 100 条、`missing=[]`；B 的重复与重启幂等键相同 | 11/11 |
| 日期无法确认 | simulated | `06a_unknown_date_daily_failed` / `06b_…last_bar_quarantined` / `06c_…prev_bar_quarantined` | CU0 realtime：日线采集失败 / 最后一条日线隔离 / 前一条日线隔离 | 三例均 `failed / SNAPSHOT_DATE_UNCONFIRMED / quality.status=unknown_date / usable=false / is_fresh=null / data_date=null`，raw 已保存，`commodity_prices` 无 realtime 行；06b/06c 错误消息含被隔离记录 ID、`validation_status=quarantined`、"no earlier bar is substituted"，`snapshot_date_resolution` 事件 `decision=rejected` 并列出两条参考日线的 ID/日期/价格/状态，`snapshot_date_unknown` WARNING 1 条 | 7/7、9/9、9/9 |
| 隔离数据 | simulated（经真实子进程边界） | `07_quarantined_via_agent` | Agent `run_once` → `fake_python.sh -m stock_data_ingestion.cli --debug fetch market-context … --trace-id corr-07-quarantine`（真实 `cli.main`，FakeAK，HSTECH 最新日线 `quarantined`） | 工具 `failed / quality.status=failed / usable=false`，值 4058.0@2026-10-05 保留供检查，1p/5p/20p 全部 `value=null + reason="upstream_validation_blocked: 2026-10-05=quarantined"`；Agent ticket `failed`、`market_context_snapshots` 0 行、覆盖 `contexts_with_snapshot=0`、`collection.result status=failed usable=false`；`agent.jsonl` 的 `agent_outcome`：`snapshot_saved=false / counted_as_coverage=false / final_status=failed`，`trace_id` 与工具 stderr 事件一致；`debug.jsonl` 为 Agent 转存的工具 stderr（含 `quality_decision / upstream_validation_blocked / request_summary`） | 16/16 |

### 7.3 回归与遗留

- 工具 `tests/test_market_context.py` 61 项，全套 **187 passed**；Agent `tests/test_v09_market_context.py` 20 项，全套 **245 passed**。
- 日志默认 INFO 只出 `request_summary`（+ 请求级 WARNING/ERROR）；DEBUG 下 100 条 FX 记录一次请求约产生 200 条明细事件
  （`record_write` + `request_record_link` 各 100），文件 20 MiB × 5 轮转。
- 真实库在本轮验证中新增：五类 1d 记录各一批（已存在键按同日更新规则处理）、1 条 CU0 realtime（`rec_924985f4…`，
  `confirmed_last_session`）、对应的请求—记录关联行。§6.6 提到的旧遗留行未动。
- 未验证（与 §6.6 相同）：盘中真实快照、供应商带完整日期的快照、真实迟到结果、真实当日变价；真实 Agent 子进程下的
  `TimeoutExpired` 部分 stderr 转存只有离线测试。
