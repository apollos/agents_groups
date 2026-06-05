# stock_data_collector / stock_data_ingestion

A 股股票结构化数据获取程序。它定位为量化系统最底层的数据底座：负责从 Tushare Pro、AKShare、JoinQuant/JQData 获取结构化数据，保存原始响应，标准化字段，执行多源校验，记录冲突，给数据质量评分，并把可追溯的标准化数据写入 SQLite / Parquet。


## 本版优化摘要（v0.2.0）

本版根据代码复评意见做了以下可靠性增强：

- `IngestionRunner` 不再只处理 `historical_bars`，已补齐 `security_master`、`trade_calendar`、`trading_status`、`realtime_quote`、`adj_factor`、`financial_statement`、`financial_indicator`、`valuation_metric`、`industry_concept`、`money_flow`、`index_data`、`corporate_action` 的统一 raw -> standard record -> response 路径；
- provider 失败、raw 保存失败、标准化失败、SQLite/Parquet 持久化失败都会生成结构化 `ErrorRecord`，并进入 `StockDataResponse.errors`；
- 标准记录启用更严格的字段级 provenance 校验：业务字段有值但缺少 `field_provenance` 会被拒绝；
- Raw Object Store metadata 写入 `raw_hash`，CLI 校验 raw hash 时优先从 SQLite `raw_payload_index` 取期望 hash，取不到时回退 raw metadata；
- 合并策略新增白名单字段级补充 `fill_missing_from_supplement`，且补充字段会记录 `source_role=supplement` 和补充原因；
- 修复质量评分中冲突严重度按字符串排序的问题，改为业务顺序 `low < medium < high < critical`；
- `Repository.insert_skip_duplicate()` 改为 savepoint 方式，重复写入不会回滚同一事务中已写入的其它对象；
- Runner 接入 `ParquetStore`，当 `export_parquet=True` 且安装 pyarrow 时导出清洗后的标准记录；
- 新增 Runner 级 fake adapter 端到端测试，覆盖非行情标准化、结构化错误传播、主源冲突不覆盖、provider-specific append、fallback_disabled、字段级补充、raw_hash metadata。

## 1. 程序定位

本程序只做四件事：

1. 数据获取；
2. 数据校验；
3. 数据存储；
4. 数据查询。

它不是 Agent，不调用 LLM，不做投资分析，不生成买入/卖出/加仓/减仓/目标价/止损价，不做风控审批，不做订单生成，不做模拟撮合，不接券商接口，不做真实交易。

后续模块应只读取本程序产生的标准化数据、原始数据索引、冲突记录和质量报告，不应绕过本程序直接访问外部数据源。

## 2. 数据源角色

三类数据源不是平等关系：

| 数据源 | 角色 | 默认用途 |
|---|---|---|
| Tushare Pro | canonical provider / 主数据源 | 生成主标准记录 |
| AKShare | validator + supplement provider | 免费公开补充、交叉验证 |
| JoinQuant/JQData | validator + supplement provider | 研究验证、回测对照、分钟线验证 |

核心原则：即使 AKShare 和 JoinQuant 同时不同意 Tushare，也不能自动覆盖 Tushare。程序只会记录冲突、标记 `canonical_value_suspect=true`，并在 high/critical 关键冲突时隔离或要求人工复核。

## 3. 非目标

本程序明确不做：

- Agent / LLM 调用；
- 投资建议；
- 新闻、公告、政策、舆情、社交平台采集；
- 行情特征工程；
- 异动事件生成；
- 交易、下单、撮合、券商接口；
- 把 AKShare / JoinQuant 无痕覆盖 Tushare；
- 把原始大字段直接塞入数据库；
- 把行业、概念、资金流等不同口径数据强行合并为唯一真值。

## 4. 安装方式

```bash
cd stock_data_collector
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev,providers]
```

只运行无外部 API 的测试时，`providers` 可不安装；真实拉取数据时需要安装对应 SDK。

```bash
pip install -e .[dev]
pytest
```

## 5. 环境变量

复制 `.env.example` 后填写真实凭证，不要提交真实 `.env`。

```bash
cp .env.example .env
export TUSHARE_TOKEN="your_tushare_token"
export JQDATA_USERNAME="your_joinquant_username"
export JQDATA_PASSWORD="your_joinquant_password"
```

凭证读取规则：

- Tushare token 从 `TUSHARE_TOKEN` 读取；
- JoinQuant 账号从 `JQDATA_USERNAME`、`JQDATA_PASSWORD` 读取；
- AKShare 通常不需要认证；
- 凭证不会硬编码，不会写入日志，不会写入 raw payload。

## 6. 配置文件

### `config/data_sources.yaml`

关键默认值：

```yaml
canonical_provider: tushare
provider_priority:
  - tushare
  - akshare
  - joinquant
validator_providers:
  - akshare
  - joinquant
supplement_providers:
  - akshare
  - joinquant
allow_field_level_merge: true
allow_fallback_when_canonical_missing: true
allow_majority_override_canonical: false
quarantine_on_critical_conflict: true
manual_review_on_trading_critical_conflict: true
minimum_quality_for_supplement: 0.65
minimum_quality_for_trading_use: 0.80
```

`allow_majority_override_canonical` 默认且必须为 `false`。

### `config/storage.yaml`

```yaml
sqlite_path: data/stock_data.db
enable_wal: true
raw_object_root: data/raw_objects
parquet_root: data/parquet
compress_raw_payload: true
raw_format: jsonl.gz
timezone: Asia/Shanghai
log_path: logs/stock_data_ingestion.log
```

### `config/data_quality.yaml`

定义字段容忍阈值、数据源初始可靠性、评分权重、冲突严重度规则、关键字段、可补充字段白名单。

## 7. 项目结构

```text
stock_data_collector/
  pyproject.toml
  README.md
  .env.example
  config/
    data_sources.yaml
    storage.yaml
    data_quality.yaml
  stock_data_ingestion/
    __init__.py
    cli.py
    config.py
    logging_config.py
    adapters/
      base.py
      tushare_adapter.py
      akshare_adapter.py
      joinquant_adapter.py
    schemas/
      requests.py
      responses.py
      records.py
      quality.py
      errors.py
    storage/
      database.py
      models.py
      repositories.py
      raw_object_store.py
      parquet_store.py
      migrations.py
    normalization/
      ticker.py
      datetime_utils.py
      units.py
      field_mapping.py
    validation/
      comparison.py
      conflict.py
      quality_score.py
      merge_policy.py
    services/
      collector.py
      query_service.py
      ingestion_runner.py
    utils/
      hashing.py
      idempotency.py
      retry.py
  tests/
    test_ticker_normalization.py
    test_idempotency.py
    test_raw_object_store.py
    test_bar_record_schema.py
    test_merge_policy.py
    test_conflict_detection.py
    test_quality_score.py
    test_sqlite_schema.py
    test_query_service.py
    test_request_response_schema.py
    test_cli.py
```

## 8. StockDataRequest

`StockDataRequest` 由 `pydantic v2` 定义，负责统一输入校验、ticker 标准化、日期范围校验和幂等键生成。

示例：

```json
{
  "request_id": "req_20260529_000001",
  "schema_version": "stock_data_request.v0.1",
  "request_type": "historical_bars",
  "tickers": ["600519.SH", "000001.SZ"],
  "names": ["贵州茅台", "平安银行"],
  "universe_id": "trading_candidates_v0",
  "market": "A_share",
  "exchanges": ["SSE", "SZSE", "BSE"],
  "start_date": "2024-01-01",
  "end_date": "2026-05-29",
  "frequency": "1d",
  "adjust": "qfq",
  "fields": ["open", "high", "low", "close", "volume", "amount"],
  "provider_priority": ["tushare", "akshare", "joinquant"],
  "canonical_provider": "tushare",
  "fallback_enabled": true,
  "cross_validate": true,
  "save_raw": true,
  "save_cleaned": true,
  "export_parquet": true,
  "requested_by": "manual",
  "created_at": "2026-05-29T10:00:00+08:00"
}
```

支持的 `request_type`：

- `security_master`
- `trade_calendar`
- `trading_status`
- `historical_bars`
- `realtime_quote`
- `adj_factor`
- `financial_statement`
- `financial_indicator`
- `valuation_metric`
- `industry_concept`
- `money_flow`
- `index_data`
- `corporate_action`
- `batch_refresh`
- `cross_validation`

支持的 `frequency`：`1m`、`5m`、`15m`、`30m`、`60m`、`1d`、`1w`、`1mo`、`realtime`。

支持的 `adjust`：`none`、`qfq`、`hfq`。

## 9. StockDataResponse

`StockDataResponse` 包含：

- 请求信息；
- provider 拉取结果；
- provider 比较结果；
- 标准化数据；
- 质量报告；
- 持久化结果；
- 结构化错误。

`status` 取值：

- `success`
- `partial_success`
- `failed`

## 10. 标准记录字段字典

所有标准记录都继承 `StandardRecord`，必须具备：

| 字段 | 含义 |
|---|---|
| `record_id` | 标准记录 ID |
| `schema_version` | 记录 schema 版本 |
| `record_type` | 记录类型 |
| `provider` | 实际返回该条数据的数据源 |
| `source_api` | 数据源 API 名称 |
| `source_site` | 数据源站点或底层来源 |
| `adapter_version` | Adapter 版本 |
| `canonical_provider` | 主源，默认 Tushare |
| `effective_provider` | 最终有效 provider |
| `source_role` | canonical / validator / supplement / fallback_canonical / provider_specific |
| `merge_method` | 合并策略 |
| `validation_status` | 校验状态 |
| `field_provenance` | 字段级来源追踪 |
| `supplement_flags` | 补充标记 |
| `conflict_ids` | 关联冲突 ID |
| `canonical_value_suspect` | 主源值是否可疑 |
| `fetch_time` | 拉取完成时间 |
| `provider_update_time` | 数据源更新时间 |
| `ingested_at` | 入库时间 |
| `request_id` | 请求 ID |
| `ingestion_run_id` | 本次执行 ID |
| `request_params_hash` | 请求参数哈希 |
| `idempotency_key` | 幂等键 |
| `raw_payload_id` | 原始数据对象 ID |
| `raw_payload_ref` | 原始数据对象引用 |
| `raw_hash` | 原始对象哈希 |
| `raw_format` | 固定为 `jsonl.gz` |
| `raw_row_index` | 原始对象中的行号 |
| `data_quality` | 数据质量分 |
| `quality_flags` | 质量标记 |

涉及股票的记录额外包含：

- `normalized_ticker`
- `provider_symbol`
- `exchange`
- `market`
- `asset_type`

涉及时间的记录包含 `timezone`；涉及金额的记录包含 `currency`；涉及行情的记录包含 `adjust`。

## 11. BarRecord 字段字典

BarRecord 是行情数据核心模型，包含：

| 字段 | 含义 |
|---|---|
| `normalized_ticker` | 内部统一代码，如 `600519.SH` |
| `provider_symbol` | provider 原始代码 |
| `exchange` | `SH` / `SZ` / `BJ` |
| `market` | 默认 `A_share` |
| `asset_type` | 默认 `stock` |
| `currency` | 默认 `CNY` |
| `trade_date` | 交易日期 |
| `timestamp` | K 线时间戳 |
| `timezone` | 默认 `Asia/Shanghai` |
| `frequency` | `1m` / `5m` / `15m` / `30m` / `60m` / `1d` / `1w` / `1mo` |
| `bar_start_time` | K 线开始时间 |
| `bar_end_time` | K 线结束时间 |
| `trading_session` | 交易时段 |
| `is_complete` | 是否完整 K 线 |
| `open` | 开盘价 |
| `high` | 最高价 |
| `low` | 最低价 |
| `close` | 收盘价 |
| `pre_close` | 前收盘价 |
| `change` | 涨跌额 |
| `pct_change` | 涨跌幅 |
| `volume` | 统一后的成交量 |
| `volume_unit` | 默认 `share` |
| `amount` | 统一后的成交额 |
| `amount_unit` | 默认 `CNY` |
| `vwap` | 成交额 / 成交量 |
| `turnover_rate` | 换手率 |
| `turnover_rate_free_float` | 自由流通换手率 |
| `adjust` | `none` / `qfq` / `hfq` |
| `adj_factor` | 复权因子 |

BarRecord 会校验：

- `adjust` 必须合法；
- high/low 价格逻辑必须合理；
- volume/amount 不能为负；
- 必须有 `field_provenance`；
- 必须能追溯到 raw payload。

## 12. Raw Object Store

所有外部数据源原始响应必须保存到 Raw Object Store。本项目第一阶段使用本地目录，未来可迁移到 S3、OSS、COS、MinIO。

约束：

- 原始数据统一为 gzip 压缩 JSON Lines；
- 扩展名固定 `.jsonl.gz`；
- 第一行是 metadata；
- 第二行以后是 raw_record；
- SQLite 不保存原始大字段，只保存 `raw_payload_index`；
- Parquet 只保存清洗后的结构化数据，不作为 raw 原始格式；
- `RawObjectStore.save_raw_payload()` 不覆盖已存在 raw object，同一 raw payload ID 会返回已有对象索引。

目录示例：

```text
data/raw_objects/
  provider=tushare/
    request_type=historical_bars/
      date=2026-05-29/
        raw_tushare_historical_bars_20260529_req_20260529_000001.jsonl.gz
```

`RawObjectStore` 提供：

- `save_raw_payload`
- `load_raw_payload`
- `compute_raw_hash`
- `verify_raw_hash`
- `build_raw_payload_ref`
- `parse_raw_payload_ref`
- `list_raw_payloads`
- `read_raw_record_by_index`

## 13. RawPayloadIndexRecord 字段字典

| 字段 | 含义 |
|---|---|
| `raw_payload_id` | 原始数据对象 ID |
| `raw_payload_ref` | 本地 raw:// 引用 |
| `provider` | provider 名称 |
| `source_api` | API 名称 |
| `source_site` | 数据来源站点 |
| `adapter_version` | Adapter 版本 |
| `request_id` | 请求 ID |
| `ingestion_run_id` | 执行 ID |
| `request_type` | 请求类型 |
| `sanitized_request_params` | 已脱敏请求参数 |
| `request_params_hash` | 请求参数哈希 |
| `idempotency_key` | 幂等键 |
| `fetch_started_at` | 开始拉取时间 |
| `fetch_completed_at` | 完成拉取时间 |
| `provider_update_time` | provider 更新时间 |
| `raw_format` | `jsonl.gz` |
| `content_encoding` | `gzip` |
| `timezone` | 时区 |
| `raw_hash` | 原始文件哈希 |
| `rows_fetched` | raw_record 行数 |

## 14. ProviderFetchResult 字段字典

| 字段 | 含义 |
|---|---|
| `provider` | 数据源 |
| `source_api` | API 名称 |
| `source_site` | 来源站点 |
| `adapter_version` | Adapter 版本 |
| `status` | success / partial_success / failed / unavailable / empty_result |
| `raw_payload_id` | 原始对象 ID |
| `raw_payload_ref` | 原始对象引用 |
| `raw_hash` | 原始对象哈希 |
| `raw_records` | Adapter 返回的行级原始数据，不直接入库大字段 |
| `rows_fetched` | 行数 |
| `started_at` | 开始时间 |
| `completed_at` | 完成时间 |
| `error` | 结构化 ErrorRecord |

代码中也提供别名 `AdapterFetchResult = ProviderFetchResult`，以符合 Adapter 层语义。

## 15. ProviderComparisonResult 字段字典

| 字段 | 含义 |
|---|---|
| `comparison_id` | 比较 ID |
| `record_type` | 记录类型 |
| `comparison_key` | 标准化比较键 |
| `canonical_provider` | 主源 |
| `compared_provider` | 对照源 |
| `status` | matched / conflicted |
| `checked_fields` | 已检查字段 |
| `matched_fields` | 一致字段 |
| `conflicted_fields` | 冲突字段 |
| `conflicts` | DataQualityConflict 列表 |
| `created_at` | 创建时间 |
| `request_id` | 请求 ID |
| `ingestion_run_id` | 执行 ID |

## 16. DataQualityConflict 字段字典

| 字段 | 含义 |
|---|---|
| `conflict_id` | 冲突 ID |
| `record_type` | 记录类型 |
| `comparison_key` | 比较键 |
| `field_name` | 冲突字段 |
| `canonical_provider` | 主源 |
| `canonical_value` | 主源值 |
| `other_provider` | 对照源 |
| `other_value` | 对照源值 |
| `severity` | low / medium / high / critical |
| `tolerance` | 容忍阈值与实际差异 |
| `reason` | 冲突原因 |
| `resolution` | 解决方式 |
| `created_at` | 创建时间 |
| `request_id` | 请求 ID |
| `ingestion_run_id` | 执行 ID |
| `canonical_record_id` | 主源记录 ID |
| `other_record_id` | 对照源记录 ID |

## 17. ErrorRecord 字段字典

| 字段 | 含义 |
|---|---|
| `error_id` | 错误 ID |
| `provider` | provider |
| `source_api` | API |
| `source_site` | 来源站点 |
| `error_code` | 错误码 |
| `error_message` | 错误消息 |
| `retryable` | 是否可重试 |
| `retry_count` | 重试次数 |
| `suggested_action` | 建议动作 |
| `created_at` | 创建时间 |

错误码包括：

- `AUTH_FAILED`
- `PERMISSION_DENIED`
- `TOKEN_MISSING`
- `RATE_LIMITED`
- `PROVIDER_TIMEOUT`
- `PROVIDER_UNAVAILABLE`
- `PROVIDER_SCHEMA_CHANGED`
- `EMPTY_RESULT`
- `INVALID_REQUEST`
- `INVALID_TICKER`
- `INVALID_DATE_RANGE`
- `NORMALIZATION_FAILED`
- `CROSS_VALIDATION_FAILED`
- `RAW_SAVE_FAILED`
- `STORAGE_FAILED`
- `IDEMPOTENCY_CONFLICT`
- `UNKNOWN_ERROR`

## 18. SQLite 表结构

使用 SQLAlchemy 2.x 定义模型，SQLite 开启 WAL。表包括：

- `securities`
- `ticker_mappings`
- `trade_calendar`
- `trading_status`
- `daily_bars`
- `weekly_bars`
- `minute_bars`
- `realtime_quotes`
- `adj_factors`
- `financial_statements`
- `financial_indicators`
- `valuation_metrics`
- `industry_memberships`
- `concept_memberships`
- `money_flow`
- `indices`
- `index_bars`
- `index_constituents`
- `corporate_actions`
- `source_fetch_logs`
- `provider_comparisons`
- `data_quality_conflicts`
- `raw_payload_index`
- `ingestion_requests`
- `ingestion_runs`

每张标准数据表包含：主键、唯一键、常用索引、`created_at`、`updated_at`、`provider`、`raw_payload_id`、`data_quality`、`validation_status`。

典型唯一键：

- Bar：`normalized_ticker + frequency + trade_date + timestamp + adjust + effective_provider`
- TradeCalendar：`exchange + calendar_date + effective_provider`
- AdjFactor：`normalized_ticker + trade_date + effective_provider`
- ValuationMetric：`normalized_ticker + trade_date + effective_provider`
- FinancialStatement：`normalized_ticker + report_period + statement_type + report_type + effective_provider`

## 19. Parquet 导出

`ParquetStore` 只用于清洗后的结构化数据，不用于 raw 原始响应。

推荐分区：

```text
data/parquet/
  bars/
    frequency=1d/
      trade_date=2026-05-29/
        part-000.parquet
  valuation_metrics/
    trade_date=2026-05-29/
      part-000.parquet
  financial_indicators/
    report_period=2025Q4/
      part-000.parquet
```

能力：

- 按表导出；
- 支持增量写入；
- 支持日期范围读取；
- 支持股票列表读取；
- 支持校验 Parquet 行数与 SQLite 行数。

## 20. 股票代码标准化

内部统一格式：

- `600519.SH`
- `000001.SZ`
- `430047.BJ`

支持输入：

- `600519.SH`
- `000001.SZ`
- `430047.BJ`
- `600519`
- `sz000001`
- `sh600519`
- `600519.XSHG`
- `000001.XSHE`

函数：

- `normalize_ticker`
- `infer_exchange`
- `to_tushare_symbol`
- `to_akshare_symbol`
- `to_joinquant_symbol`
- `validate_a_share_ticker`

无法识别时抛出包含 `INVALID_TICKER` 的结构化异常，不静默猜测。

## 21. 时间、单位和复权

`datetime_utils.py` 提供：

- `normalize_trade_date`
- `normalize_timestamp`
- `infer_bar_start_end`
- `to_asia_shanghai`
- `validate_date_range`
- `build_quote_time_bucket`

`units.py` 提供：

- `normalize_volume`
- `normalize_amount`
- `normalize_currency`
- `compute_vwap`
- `normalize_turnover_rate`

复权方式统一为：

- `none`
- `qfq`
- `hfq`

BarRecord 必须显式写入 `adjust`。

## 22. 多源比较与冲突处理

比较前先标准化：股票代码、日期、时间、频率、复权方式、成交量单位、成交额单位、币种。

核心比较键：

| 记录 | comparison_key |
|---|---|
| SecurityMasterRecord | `normalized_ticker` |
| TradeCalendarRecord | `exchange + calendar_date` |
| TradingStatusRecord | `normalized_ticker + trade_date` |
| BarRecord | `normalized_ticker + frequency + trade_date/timestamp + adjust` |
| RealtimeQuoteRecord | `normalized_ticker + quote_time_bucket` |
| AdjFactorRecord | `normalized_ticker + trade_date` |
| FinancialStatementRecord | `normalized_ticker + report_period + statement_type + report_type` |
| FinancialIndicatorRecord | `normalized_ticker + report_period` |
| ValuationMetricRecord | `normalized_ticker + trade_date` |
| IndustryMembershipRecord | `normalized_ticker + industry_system + effective_date` |
| ConceptMembershipRecord | `normalized_ticker + concept_code/concept_name + provider` |
| MoneyFlowRecord | `normalized_ticker + trade_date + frequency + source_methodology` |
| IndexConstituentRecord | `index_code + normalized_ticker + effective_date` |
| CorporateActionRecord | `normalized_ticker + action_type + announcement_date + ex_date` |

数值容忍阈值默认实现：

- 价格：绝对差异 ≤ 0.01 或相对差异 ≤ 0.01%；
- 成交量：相对差异 ≤ 0.5%；
- 成交额：相对差异 ≤ 1.0%；
- 换手率：绝对差异 ≤ 0.05 个百分点；
- 复权因子：相对差异 ≤ 0.01%；
- 市值：相对差异 ≤ 1.0%；
- PE / PB / PS：相对差异 ≤ 1.0%；
- 财报金额：相对差异 ≤ 0.1% 或绝对差异低于阈值；
- 财务比率：绝对差异 ≤ 0.01 或相对差异 ≤ 1.0%。

严重度：

- `low`：小数精度差异、公司名称格式差异、轻微四舍五入差异；
- `medium`：成交量/成交额/估值/财务指标明显差异；
- `high`：收盘价、复权因子、停复牌、ST、涨跌停、交易日历、财报核心字段明显不一致；
- `critical`：主源显示可交易但辅助源显示停牌，主源非 ST 但辅助源 ST/*ST，主源未跌停但辅助源跌停，价格严重不合理，复权因子导致收益曲线严重异常，财报核心字段数量级不一致。

critical 冲突数据不得进入可交易数据层。

## 23. 合并策略

实现的 `merge_method`：

- `canonical_only`
- `canonical_validated`
- `canonical_with_warning`
- `fill_missing_from_supplement`
- `fallback_single_source`
- `fallback_multi_source_agreed`
- `provider_specific_append`
- `quarantined_due_to_conflict`
- `manual_review_required`

规则：

1. Tushare 有数据且辅助源一致：使用 Tushare，`canonical_validated`，质量分提高；
2. Tushare 有数据但辅助源不一致：保留 Tushare，记录冲突，不自动覆盖；关键 high/critical 冲突隔离或人工复核；
3. Tushare 缺失但辅助源有数据：只允许白名单字段或 fallback 记录补充，质量分不能满分；
4. 行业、概念、资金流等口径不同数据：`provider_specific_append`，按 provider 分别保存。

## 24. 数据质量评分

公式：

```text
data_quality_score =
  0.25 * completeness_score
+ 0.30 * consistency_score
+ 0.15 * timeliness_score
+ 0.10 * provider_reliability_score
+ 0.10 * anomaly_score
+ 0.10 * provenance_score
```

初始 provider 可靠性：

| provider | reliability |
|---|---:|
| tushare | 0.90 |
| joinquant | 0.85 |
| akshare | 0.75 |
| manual_import | 0.60 |
| unknown | 0.30 |

调整规则：

| 规则 | 调整 |
|---|---:|
| canonical_validated_by_two_sources | +0.08 |
| canonical_validated_by_one_source | +0.04 |
| canonical_only_not_validated | -0.05 |
| single_source_fallback | -0.15 |
| multi_source_fallback_agreed | -0.08 |
| low_conflict | -0.05 |
| medium_conflict | -0.12 |
| high_conflict | -0.25 |
| critical_conflict | -0.50 |
| missing_raw_payload_ref | -0.30 |
| missing_field_provenance | -0.20 |

所有分数限制在 0 到 1。

## 25. 命令行示例

初始化数据库：

```bash
python -m stock_data_ingestion.cli init-db
```

拉取股票基础信息：

```bash
python -m stock_data_ingestion.cli fetch security-master --tickers 600519.SH 000001.SZ
```

拉取交易日历：

```bash
python -m stock_data_ingestion.cli fetch trade-calendar --exchange SSE --start-date 2024-01-01 --end-date 2026-05-29
```

拉取历史行情：

```bash
python -m stock_data_ingestion.cli fetch historical-bars \
  --tickers 600519.SH 000001.SZ \
  --start-date 2024-01-01 \
  --end-date 2026-05-29 \
  --frequency 1d \
  --adjust qfq \
  --cross-validate
```

拉取估值指标：

```bash
python -m stock_data_ingestion.cli fetch valuation --tickers 600519.SH --start-date 2024-01-01 --end-date 2026-05-29
```

拉取财务指标：

```bash
python -m stock_data_ingestion.cli fetch financial-indicator --tickers 600519.SH --start-date 2020-01-01 --end-date 2026-05-29
```

查询行情：

```bash
python -m stock_data_ingestion.cli query bars --ticker 600519.SH --start-date 2024-01-01 --end-date 2026-05-29 --frequency 1d --adjust qfq
```

查询冲突：

```bash
python -m stock_data_ingestion.cli query conflicts --ticker 600519.SH
```

校验 raw hash：

```bash
python -m stock_data_ingestion.cli verify raw --raw-payload-id raw_tushare_daily_20260529_req_000001 --expected-hash sha256:...
```

## 26. 查询服务示例

```python
from stock_data_ingestion.storage.database import Database
from stock_data_ingestion.services.query_service import QueryService

_db = Database("data/stock_data.db")
with _db.session() as session:
    qs = QueryService(session)
    bars = qs.get_bars("600519.SH", "2024-01-01", "2026-05-29", "1d", "qfq")
    conflicts = qs.get_conflicts("600519.SH")
```

QueryService 支持：

1. 按股票代码查询基础信息；
2. 按股票列表查询基础信息；
3. 按股票代码、日期范围、频率、复权方式查询历史行情；
4. 按股票代码、交易日查询估值指标；
5. 按股票代码、报告期查询财务指标；
6. 按股票代码查询行业归属；
7. 按股票代码查询概念归属；
8. 按股票代码、日期范围查询资金流；
9. 按指数代码查询指数成分；
10. 按 raw_payload_id 查询原始数据索引；
11. 按 record_id 反查 raw_payload_ref 和 raw_row_index；
12. 查询 data_quality_conflicts；
13. 查询某个数据是否 `validation_status = quarantined`；
14. 查询某条记录的 `field_provenance`。

查询返回 pandas DataFrame 或字典，并保留来源字段。

## 27. Adapter 设计

`BaseDataAdapter` 定义统一接口：

- `provider_name`
- `adapter_version`
- `is_available`
- `authenticate`
- `fetch_security_master`
- `fetch_trade_calendar`
- `fetch_trading_status`
- `fetch_historical_bars`
- `fetch_realtime_quote`
- `fetch_adj_factor`
- `fetch_financial_statement`
- `fetch_financial_indicator`
- `fetch_valuation_metric`
- `fetch_industry_membership`
- `fetch_concept_membership`
- `fetch_money_flow`
- `fetch_index_data`
- `fetch_corporate_action`
- `normalize_raw_data`
- `map_provider_symbol_to_normalized_ticker`
- `map_normalized_ticker_to_provider_symbol`

每个 fetch 方法都返回 `ProviderFetchResult` / `AdapterFetchResult`，上层服务不直接接收 pandas DataFrame。

## 28. 测试方式

测试不依赖真实外部 API。Fake / 固定数据用于校验标准化、幂等、Raw Object Store、冲突、质量评分、CLI 参数等。

```bash
pytest
```

本版代码复核时已运行：

```text
35 tests collected
33 passed
2 skipped
```

两个 skipped 测试是当前执行环境未安装 `SQLAlchemy` 时自动跳过；安装 `SQLAlchemy>=2.0` 后会执行 SQLite schema 和 QueryService 测试。当前环境也未安装 `pyarrow`，因此 Parquet 运行时能力在依赖完整环境中执行。

## 29. 已知限制

- 真实 provider API 的字段在未来可能变化，AKShare 字段变化会返回 `PROVIDER_SCHEMA_CHANGED`；
- JQData 财务指标示例保守留空，生产环境可在 Adapter 内扩展具体字段，但上层服务不得直接调用 JQData；
- 当前 Raw Object Store 为本地目录实现；迁移对象存储时应保持 `raw_payload_ref` 抽象；
- Parquet 写入依赖 `pyarrow`；
- 本项目已打通所有 request_type 的统一标准化入口和 response 路径，但真实 provider 的不同权限、不同 SDK 版本可能导致字段命名差异；生产接入前应按实际 Tushare/AKShare/JQData 权限继续补齐和固化 provider-specific 字段映射；
- 当前执行环境如未安装 `SQLAlchemy` / `pyarrow`，数据库和 Parquet 相关测试会自动跳过；完整依赖环境下应纳入 CI 强制执行。

## 30. 后续扩展方向

- 对每类 request_type 补齐 provider-specific 字段映射；
- 增加 Alembic 迁移；
- 增加对象存储后端；
- 增加调度器接入层，但调度器不应绕过 Adapter 和 Raw Object Store；
- 增加更多财务报表字段和指数数据字段；
- 增加数据质量 Dashboard；
- 增加人工复核工作流，但不改变“不得自动覆盖主源”的原则。
