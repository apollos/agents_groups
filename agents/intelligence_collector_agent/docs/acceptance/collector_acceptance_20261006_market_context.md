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

## 1. 工具侧真实样本（CLI 直接调用，2026-10-06）

命令形如 `python -m stock_data_ingestion.cli --config-dir config fetch market-context --context-type <T> --symbol <S> --as-of 2026-10-06 --compact`。

| # | 类别 / 代码 | 命中接口 | `data_date` | 取值 | 单位 | 新鲜度 | 变动口径 |
|---|---|---|---|---|---|---|---|
| 1.1 | `fx` HKDCNY（`spot_sell`） | `currency_boc_sina`（中行牌价） | 2026-10-06 | 85.59 | CNY per 100 HKD | fresh, 0d | percent, period_unit=observation |
| 1.2 | `hk_index` HSTECH | `stock_hk_index_daily_sina` | 2026-10-05 | 4183.68018 | index_points | fresh, 1d | percent, trading_day |
| 1.3 | `commodity` CU0 futures 1d | `futures_zh_daily_sina` | 2026-09-30 | close 109680 / settle 109570 | CNY/ton | stale, 6d（tolerance 3d）→ `partial_success` | percent, trading_day |
| 1.4 | `commodity` CU0 realtime | `futures_zh_spot` | 2026-09-30（由同合约日线确认，`snapshot_date_confidence=corrected_to_last_session`，warning `snapshot_belongs_to_previous_session`） | latest 109680 | CNY/ton | stale | `snapshot_vs_pre_settle` +0.3477%；1/5/20p 来自已确认日线 |
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
3. 盘中 `futures_zh_spot` 实时快照的"当日归属 + 确认中"路径（`confirmed_live_session`）仅有离线测试覆盖，需在交易时段补一次真实验证。
