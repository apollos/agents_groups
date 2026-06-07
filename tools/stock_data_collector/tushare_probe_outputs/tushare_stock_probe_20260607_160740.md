# Tushare Stock Coverage Probe Report

Generated at: `2026-06-07T16:07:40`

## Run configuration

- `tickers`: `['600519.SH', '000001.SZ']`
- `start_date`: `20260207`
- `end_date`: `20260607`
- `financial_start_date`: `20210608`
- `financial_end_date`: `20260607`
- `event_start_date`: `20210608`
- `event_end_date`: `20260607`
- `calendar_start_date`: `20260508`
- `calendar_end_date`: `20260707`
- `calendar_exchanges`: `['SSE', 'SZSE', 'BSE']`
- `env_file_loaded`: `/mnt/d/workspace/agents_groups/tools/stock_data_collector/.env`
- `tushare_token_present`: `True`
- `scopes`: `None`

## Summary

- Overall status: **NEEDS_REVIEW**
- Total checks: **32**
- Problematic checks: **7**
- Status counts: `{"PASS": 25, "EMPTY": 7}`

## Coverage table

| Status | Scope | API | Ticker/Exchange | Rows | Missing columns | Hint |
|---|---|---|---:|---:|---|---|
| PASS | adj_factor | adj_factor | 000001.SZ | 75 |  |  |
| PASS | adj_factor | adj_factor | 600519.SH | 75 |  |  |
| PASS | corporate_action_repurchase | repurchase | 600519.SH,000001.SZ | 4 |  |  |
| PASS | financial_indicator | fina_indicator | 000001.SZ | 30 |  |  |
| PASS | financial_indicator | fina_indicator | 600519.SH | 31 |  |  |
| PASS | financial_statement_balancesheet | balancesheet | 000001.SZ | 31 |  |  |
| PASS | financial_statement_balancesheet | balancesheet | 600519.SH | 24 |  |  |
| PASS | financial_statement_cashflow | cashflow | 000001.SZ | 25 |  |  |
| PASS | financial_statement_cashflow | cashflow | 600519.SH | 28 |  |  |
| PASS | financial_statement_income | income | 000001.SZ | 23 |  |  |
| PASS | financial_statement_income | income | 600519.SH | 25 |  |  |
| PASS | historical_bars_daily | daily | 000001.SZ | 75 |  |  |
| PASS | historical_bars_daily | daily | 600519.SH | 75 |  |  |
| PASS | money_flow | moneyflow | 000001.SZ | 75 |  |  |
| PASS | money_flow | moneyflow | 600519.SH | 75 |  |  |
| PASS | security_master | stock_basic | 000001.SZ | 1 |  |  |
| PASS | security_master | stock_basic | 600519.SH | 1 |  |  |
| PASS | security_master_company_profile | stock_company | 000001.SZ | 1 |  |  |
| PASS | security_master_company_profile | stock_company | 600519.SH | 1 |  |  |
| PASS | trade_calendar | trade_cal | SSE | 61 |  |  |
| PASS | trade_calendar | trade_cal | SZSE | 61 |  |  |
| PASS | trading_status_limit_price | stk_limit | 000001.SZ | 75 |  |  |
| PASS | trading_status_limit_price | stk_limit | 600519.SH | 75 |  |  |
| PASS | valuation_metric_daily_basic | daily_basic | 000001.SZ | 75 |  |  |
| PASS | valuation_metric_daily_basic | daily_basic | 600519.SH | 75 |  |  |
| EMPTY | corporate_action_dividend | dividend | 000001.SZ | 0 |  | 所选公告/实施日期区间内可能没有分红送股事件；扩大 --event-start-date。 |
| EMPTY | corporate_action_dividend | dividend | 600519.SH | 0 |  | 所选公告/实施日期区间内可能没有分红送股事件；扩大 --event-start-date。 |
| EMPTY | corporate_action_share_float | share_float | 000001.SZ | 0 |  | 限售股解禁是事件数据，区间内没有事件不代表接口失败。 |
| EMPTY | corporate_action_share_float | share_float | 600519.SH | 0 |  | 限售股解禁是事件数据，区间内没有事件不代表接口失败。 |
| EMPTY | trade_calendar | trade_cal | BSE | 0 |  | 空结果：需要确认日期区间、ticker、权限或该类事件是否确实不存在。 |
| EMPTY | trading_status_suspend | suspend_d | 000001.SZ | 0 |  | 空结果通常表示该股票在区间内没有停复牌事件，不一定是接口失败。 |
| EMPTY | trading_status_suspend | suspend_d | 600519.SH | 0 |  | 空结果通常表示该股票在区间内没有停复牌事件，不一定是接口失败。 |

## Failed / missing / empty details

### EMPTY: corporate_action_dividend / dividend / 000001.SZ

- Params: `{"ts_code": "000001.SZ", "start_date": "20210608", "end_date": "20260607"}`
- Rows: `0`
- Columns: `ts_code, end_date, ann_date, div_proc, stk_div, stk_bo_rate, stk_co_rate, cash_div, cash_div_tax, record_date, ex_date, pay_date, div_listdate, imp_ann_date, base_date, base_share`
- Hint: 所选公告/实施日期区间内可能没有分红送股事件；扩大 --event-start-date。

### EMPTY: corporate_action_dividend / dividend / 600519.SH

- Params: `{"ts_code": "600519.SH", "start_date": "20210608", "end_date": "20260607"}`
- Rows: `0`
- Columns: `ts_code, end_date, ann_date, div_proc, stk_div, stk_bo_rate, stk_co_rate, cash_div, cash_div_tax, record_date, ex_date, pay_date, div_listdate, imp_ann_date, base_date, base_share`
- Hint: 所选公告/实施日期区间内可能没有分红送股事件；扩大 --event-start-date。

### EMPTY: corporate_action_share_float / share_float / 000001.SZ

- Params: `{"ts_code": "000001.SZ", "start_date": "20210608", "end_date": "20260607"}`
- Rows: `0`
- Columns: `ts_code, ann_date, float_date, float_share, float_ratio, holder_name, share_type`
- Hint: 限售股解禁是事件数据，区间内没有事件不代表接口失败。

### EMPTY: corporate_action_share_float / share_float / 600519.SH

- Params: `{"ts_code": "600519.SH", "start_date": "20210608", "end_date": "20260607"}`
- Rows: `0`
- Columns: `ts_code, ann_date, float_date, float_share, float_ratio, holder_name, share_type`
- Hint: 限售股解禁是事件数据，区间内没有事件不代表接口失败。

### EMPTY: trade_calendar / trade_cal / BSE

- Params: `{"exchange": "BSE", "start_date": "20260508", "end_date": "20260707"}`
- Rows: `0`
- Columns: `exchange, cal_date, is_open, pretrade_date`
- Hint: 空结果：需要确认日期区间、ticker、权限或该类事件是否确实不存在。

### EMPTY: trading_status_suspend / suspend_d / 000001.SZ

- Params: `{"ts_code": "000001.SZ", "start_date": "20260207", "end_date": "20260607"}`
- Rows: `0`
- Columns: `ts_code, trade_date, suspend_timing, suspend_type`
- Hint: 空结果通常表示该股票在区间内没有停复牌事件，不一定是接口失败。

### EMPTY: trading_status_suspend / suspend_d / 600519.SH

- Params: `{"ts_code": "600519.SH", "start_date": "20260207", "end_date": "20260607"}`
- Rows: `0`
- Columns: `ts_code, trade_date, suspend_timing, suspend_type`
- Hint: 空结果通常表示该股票在区间内没有停复牌事件，不一定是接口失败。

## Items intentionally not probed in this stock-only script

- **realtime_quote**: 本脚本先诊断 Tushare Pro 的股票基础、日线、财务、资金流、公司行为等批量/历史接口；实时行情需另行确认具体 Tushare 实时接口、权限和返回结构。
- **minute_bars**: 分钟线接口在不同 Tushare 版本/权限下差异较大；应在确认 exact API 后单独增加探针。
- **concept_membership**: 概念板块通常不是 stock_basic 的直接字段，需要选择 THS/DC/TDX 等板块接口后再做成分映射。
- **index_data**: 本脚本按“只针对股票 ticker”设计，不测试指数基础信息、指数行情和指数成分。
