# AKShare Stock Probe

Generated at: `2026-06-07T18:14:18.797314`

## Summary

- **EMPTY**: 4
- **FAILED**: 10
- **PASS**: 18
- **SKIPPED**: 2

## Results

| name | ticker | api | status | rows | missing_columns | message |
|---|---:|---|---:|---:|---|---|
| security_master_code_name |  | `stock_info_a_code_name` | PASS | 5525 |  |  |
| security_master_sh_list |  | `stock_info_sh_name_code` | PASS | 1705 |  |  |
| security_master_sz_list |  | `stock_info_sz_name_code` | PASS | 2892 |  |  |
| security_master_bj_list |  | `stock_info_bj_name_code` | PASS | 318 |  |  |
| security_master_individual | 600519.SH | `stock_individual_info_em` | FAILED | 0 |  | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |
| historical_bars_daily | 600519.SH | `stock_zh_a_hist` | FAILED | 0 |  | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |
| historical_bars_minute_5m | 600519.SH | `stock_zh_a_hist_min_em` | FAILED | 0 |  | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |
| valuation_metric | 600519.SH | `stock_value_em` | PASS | 2042 |  |  |
| financial_statement_income | 600519.SH | `stock_profit_sheet_by_report_em` | PASS | 102 |  |  |
| financial_statement_balance | 600519.SH | `stock_balance_sheet_by_report_em` | PASS | 102 |  |  |
| financial_statement_cashflow | 600519.SH | `stock_cash_flow_sheet_by_report_em` | PASS | 98 |  |  |
| financial_indicator_em | 600519.SH | `stock_financial_analysis_indicator_em` | PASS | 102 |  |  |
| money_flow | 600519.SH | `stock_individual_fund_flow` | FAILED | 0 |  | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |
| corporate_action_dividend | 600519.SH | `stock_history_dividend_detail` | PASS | 31 |  |  |
| corporate_action_rights_issue | 600519.SH | `stock_history_dividend_detail` | EMPTY | 0 | 公告日期 |  |
| security_master_individual | 000001.SZ | `stock_individual_info_em` | FAILED | 0 |  | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |
| historical_bars_daily | 000001.SZ | `stock_zh_a_hist` | FAILED | 0 |  | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |
| historical_bars_minute_5m | 000001.SZ | `stock_zh_a_hist_min_em` | FAILED | 0 |  | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |
| valuation_metric | 000001.SZ | `stock_value_em` | PASS | 2042 |  |  |
| financial_statement_income | 000001.SZ | `stock_profit_sheet_by_report_em` | PASS | 121 |  |  |
| financial_statement_balance | 000001.SZ | `stock_balance_sheet_by_report_em` | PASS | 118 |  |  |
| financial_statement_cashflow | 000001.SZ | `stock_cash_flow_sheet_by_report_em` | PASS | 102 |  |  |
| financial_indicator_em | 000001.SZ | `stock_financial_analysis_indicator_em` | PASS | 121 |  |  |
| money_flow | 000001.SZ | `stock_individual_fund_flow` | FAILED | 0 |  | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |
| corporate_action_dividend | 000001.SZ | `stock_history_dividend_detail` | PASS | 37 |  |  |
| corporate_action_rights_issue | 000001.SZ | `stock_history_dividend_detail` | PASS | 3 |  |  |
| realtime_quote |  | `stock_zh_a_spot_em` | FAILED | 0 |  | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |
| trading_status_st |  | `stock_zh_a_st_em` | FAILED | 0 |  | ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) |
| trading_status_suspend:20250630 |  | `stock_tfp_em` | EMPTY | 0 |  |  |
| trading_status_suspend:20250629 |  | `stock_tfp_em` | EMPTY | 0 |  |  |
| trading_status_suspend:20250628 |  | `stock_tfp_em` | EMPTY | 0 |  |  |
| industry_concept |  | `stock_board_*` | SKIPPED | 0 |  | Pass --include-boards to probe board membership; this can be slow. |
| corporate_action_repurchase |  | `stock_repurchase_em` | PASS | 2 |  |  |
| adj_factor |  | `N/A` | SKIPPED | 0 |  | AKShare has adjusted prices via stock_zh_a_hist but no standalone adj-factor table compatible with the canonical schema. |