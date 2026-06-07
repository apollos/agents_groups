# Tushare EMPTY Debug Report

Generated at: `2026-06-07T14:52:26`

## Config
- `tickers`: `['600519.SH', '000001.SZ']`
- `start_date`: `20251209`
- `end_date`: `20260607`
- `event_start_date`: `19900101`
- `event_end_date`: `20260607`
- `calendar_start_date`: `20251209`
- `calendar_end_date`: `20260607`
- `known_suspend_date`: `20200312`
- `env_loaded`: `/mnt/d/workspace/agents_groups/tools/stock_data_collector/.env`
- `tushare_token_present`: `True`

## Findings
- daily_basic：估值/股本/市值字段可正常获取；不需要因为 total_share/float_share 缺失而改 Tushare daily_basic 调用。
- dividend/000001.SZ：ts_code + start_date/end_date 返回空，但 ts_code-only 返回 53 行；说明 dividend 不能按 start_date/end_date 这样筛，应该先按 ts_code 获取后本地按 ann_date/record_date/ex_date/imp_ann_date/pay_date 过滤。
- dividend/600519.SH：ts_code + start_date/end_date 返回空，但 ts_code-only 返回 58 行；说明 dividend 不能按 start_date/end_date 这样筛，应该先按 ts_code 获取后本地按 ann_date/record_date/ex_date/imp_ann_date/pay_date 过滤。
- share_float/000001.SZ：近区间为空，但 ts_code-only 返回 214 行；说明接口可取，近区间没有解禁事件或日期过滤字段不匹配。
- share_float/600519.SH：近区间为空，但 ts_code-only 返回 11 行；说明接口可取，近区间没有解禁事件或日期过滤字段不匹配。
- suspend_d/000001.SZ：长区间返回 222 行；近区间为空只是近期没有停复牌。
- suspend_d/600519.SH：长区间返回 100 行；近区间为空只是近期没有停复牌。
- suspend_d：已用一个全市场历史日期验证接口本身可返回数据；个股为空一般代表无事件。
- trade_cal/BSE：BSE 返回空，而 SSE/SZSE 正常；这更像是 Tushare trade_cal 不支持 BSE 参数，而不是交易日历整体失败。
- stock_basic(exchange='BSE') 能返回北交所股票但 trade_cal(exchange='BSE') 为空时，可确认是 trade_cal 参数支持问题，而不是 BSE 市场不存在。

## Recommended code changes
- Tushare trade_calendar 不要把 BSE 当作必然可用的 exchange 参数；BSE 可用 exchange='' 或 SSE/SZSE 日历派生，或标记 source_api 不支持。
- TushareAdapter.fetch_corporate_action(dividend) 不应向 pro.dividend 传 start_date/end_date；应传 ts_code，再在本地按 ann_date/record_date/ex_date/imp_ann_date/pay_date 做区间过滤。
- share_float 可保留 start_date/end_date 参数；但 EMPTY 应作为 event_absent，而不是 provider_failed。必要时也可 ts_code-only 回填全历史后本地过滤。

## Case table
| Status | Case | API | Ticker/Exchange | Rows | Params | Date ranges | Filtered counts | Hint |
|---|---|---|---:|---:|---|---|---|---|
| PASS | daily_basic:600519.SH | daily_basic | 600519.SH | 117 | `{"end_date": "20260607", "fields": "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv", "start_date": "20251209", "ts_code": "600519.SH"}` | `{"trade_date": {"max": "20260605", "min": "20251209", "non_null": 117, "unique": 117}}` | `{"trade_date": 117}` |  |
| PASS | daily_basic:000001.SZ | daily_basic | 000001.SZ | 117 | `{"end_date": "20260607", "fields": "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv", "start_date": "20251209", "ts_code": "000001.SZ"}` | `{"trade_date": {"max": "20260605", "min": "20251209", "non_null": 117, "unique": 117}}` | `{"trade_date": 117}` |  |
| EMPTY | dividend_with_start_end:600519.SH | dividend | 600519.SH | 0 | `{"end_date": "20260607", "fields": "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,imp_ann_date,base_date,base_share", "start_date": "19900101", "ts_code": "600519.SH"}` | `{}` | `{}` | If ts_code-only returns rows, start_date/end_date are not valid dividend filters. |
| PASS | dividend_ts_code_only:600519.SH | dividend | 600519.SH | 58 | `{"fields": "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,imp_ann_date,base_date,base_share", "ts_code": "600519.SH"}` | `{"ann_date": {"max": "20260417", "min": "20020417", "non_null": 58, "unique": 38}, "base_date": {"max": "20260603", "min": "20011231", "non_null": 50, "unique": 38}, "div_listdate": {"max": "20150720", "min": "20020726", "non_null": 8, "unique": 8}, "end_date": {"max": "20251231", "min": "20011231", "non_null": 58, "unique": 38}, "ex_date": {"max": "20251219", "min": "20020725", "non_null": 29, "unique": 29}, "imp_ann_date": {"max": "20251211", "min": "20020718", "non_null": 29, "unique": 29}, "pay_date": {"max": "20251219", "min": "20020730", "non_null": 29, "unique": 29}, "record_date": {"max": "20251218", "min": "20020724", "non_null": 29, "unique": 29}}` | `{"ann_date": 58, "base_date": 50, "div_listdate": 8, "end_date": 58, "ex_date": 29, "imp_ann_date": 29, "pay_date": 29, "record_date": 29}` |  |
| EMPTY | dividend_with_start_end:000001.SZ | dividend | 000001.SZ | 0 | `{"end_date": "20260607", "fields": "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,imp_ann_date,base_date,base_share", "start_date": "19900101", "ts_code": "000001.SZ"}` | `{}` | `{}` | If ts_code-only returns rows, start_date/end_date are not valid dividend filters. |
| PASS | dividend_ts_code_only:000001.SZ | dividend | 000001.SZ | 53 | `{"fields": "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,imp_ann_date,base_date,base_share", "ts_code": "000001.SZ"}` | `{"ann_date": {"max": "20260321", "min": "19960314", "non_null": 47, "unique": 29}, "base_date": {"max": "20260605", "min": "19901231", "non_null": 47, "unique": 35}, "div_listdate": {"max": "20160616", "min": "19910502", "non_null": 13, "unique": 13}, "end_date": {"max": "20251231", "min": "19901231", "non_null": 53, "unique": 35}, "ex_date": {"max": "20260612", "min": "19910502", "non_null": 29, "unique": 29}, "imp_ann_date": {"max": "20260605", "min": "19930509", "non_null": 27, "unique": 27}, "pay_date": {"max": "20260612", "min": "19930524", "non_null": 26, "unique": 26}, "record_date": {"max": "20260611", "min": "19910430", "non_null": 29, "unique": 29}}` | `{"ann_date": 47, "base_date": 47, "div_listdate": 13, "end_date": 53, "ex_date": 28, "imp_ann_date": 27, "pay_date": 25, "record_date": 28}` |  |
| EMPTY | share_float_recent:600519.SH | share_float | 600519.SH | 0 | `{"end_date": "20260607", "fields": "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type", "start_date": "20251209", "ts_code": "600519.SH"}` | `{}` | `{}` | No unlock events in recent interval is normal for many stocks. |
| PASS | share_float_long_range:600519.SH | share_float | 600519.SH | 11 | `{"end_date": "20260607", "fields": "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type", "start_date": "19900101", "ts_code": "600519.SH"}` | `{"ann_date": {"max": "20090519", "min": "20070518", "non_null": 11, "unique": 3}, "float_date": {"max": "20090525", "min": "20070525", "non_null": 11, "unique": 3}}` | `{"ann_date": 11, "float_date": 11}` |  |
| PASS | share_float_ts_code_only:600519.SH | share_float | 600519.SH | 11 | `{"fields": "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type", "ts_code": "600519.SH"}` | `{"ann_date": {"max": "20090519", "min": "20070518", "non_null": 11, "unique": 3}, "float_date": {"max": "20090525", "min": "20070525", "non_null": 11, "unique": 3}}` | `{"ann_date": 11, "float_date": 11}` |  |
| EMPTY | share_float_recent:000001.SZ | share_float | 000001.SZ | 0 | `{"end_date": "20260607", "fields": "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type", "start_date": "20251209", "ts_code": "000001.SZ"}` | `{}` | `{}` | No unlock events in recent interval is normal for many stocks. |
| PASS | share_float_long_range:000001.SZ | share_float | 000001.SZ | 214 | `{"end_date": "20260607", "fields": "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type", "start_date": "19900101", "ts_code": "000001.SZ"}` | `{"ann_date": {"max": "20150520", "min": "20080625", "non_null": 211, "unique": 8}, "float_date": {"max": "20180521", "min": "20080618", "non_null": 214, "unique": 10}}` | `{"ann_date": 211, "float_date": 214}` |  |
| PASS | share_float_ts_code_only:000001.SZ | share_float | 000001.SZ | 214 | `{"fields": "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type", "ts_code": "000001.SZ"}` | `{"ann_date": {"max": "20150520", "min": "20080625", "non_null": 211, "unique": 8}, "float_date": {"max": "20180521", "min": "20080618", "non_null": 214, "unique": 10}}` | `{"ann_date": 211, "float_date": 214}` |  |
| EMPTY | suspend_recent:600519.SH | suspend_d | 600519.SH | 0 | `{"end_date": "20260607", "fields": "ts_code,trade_date,suspend_timing,suspend_type", "start_date": "20251209", "ts_code": "600519.SH"}` | `{}` | `{}` | No recent suspend/resume event is normal. |
| PASS | suspend_long_range:600519.SH | suspend_d | 600519.SH | 100 | `{"end_date": "20260607", "fields": "ts_code,trade_date,suspend_timing,suspend_type", "start_date": "19900101", "ts_code": "600519.SH"}` | `{"trade_date": {"max": "20121211", "min": "20011205", "non_null": 100, "unique": 100}}` | `{"trade_date": 100}` |  |
| PASS | suspend_ts_code_only:600519.SH | suspend_d | 600519.SH | 100 | `{"fields": "ts_code,trade_date,suspend_timing,suspend_type", "ts_code": "600519.SH"}` | `{"trade_date": {"max": "20121211", "min": "20011205", "non_null": 100, "unique": 100}}` | `{"trade_date": 100}` |  |
| EMPTY | suspend_recent:000001.SZ | suspend_d | 000001.SZ | 0 | `{"end_date": "20260607", "fields": "ts_code,trade_date,suspend_timing,suspend_type", "start_date": "20251209", "ts_code": "000001.SZ"}` | `{}` | `{}` | No recent suspend/resume event is normal. |
| PASS | suspend_long_range:000001.SZ | suspend_d | 000001.SZ | 222 | `{"end_date": "20260607", "fields": "ts_code,trade_date,suspend_timing,suspend_type", "start_date": "19900101", "ts_code": "000001.SZ"}` | `{"trade_date": {"max": "20140716", "min": "19990528", "non_null": 222, "unique": 222}}` | `{"trade_date": 222}` |  |
| PASS | suspend_ts_code_only:000001.SZ | suspend_d | 000001.SZ | 222 | `{"fields": "ts_code,trade_date,suspend_timing,suspend_type", "ts_code": "000001.SZ"}` | `{"trade_date": {"max": "20140716", "min": "19990528", "non_null": 222, "unique": 222}}` | `{"trade_date": 222}` |  |
| PASS | suspend_known_date_all_market | suspend_d |  | 21 | `{"fields": "ts_code,trade_date,suspend_timing,suspend_type", "suspend_type": "S", "trade_date": "20200312"}` | `{"trade_date": {"max": "20200312", "min": "20200312", "non_null": 21, "unique": 1}}` | `{"trade_date": 21}` |  |
| PASS | trade_cal:blank | trade_cal |  | 181 | `{"end_date": "20260607", "exchange": "", "fields": "exchange,cal_date,is_open,pretrade_date", "start_date": "20251209"}` | `{}` | `{}` |  |
| PASS | trade_cal:SSE | trade_cal | SSE | 181 | `{"end_date": "20260607", "exchange": "SSE", "fields": "exchange,cal_date,is_open,pretrade_date", "start_date": "20251209"}` | `{}` | `{}` |  |
| PASS | trade_cal:SZSE | trade_cal | SZSE | 181 | `{"end_date": "20260607", "exchange": "SZSE", "fields": "exchange,cal_date,is_open,pretrade_date", "start_date": "20251209"}` | `{}` | `{}` |  |
| EMPTY | trade_cal:BSE | trade_cal | BSE | 0 | `{"end_date": "20260607", "exchange": "BSE", "fields": "exchange,cal_date,is_open,pretrade_date", "start_date": "20251209"}` | `{}` | `{}` | This exchange parameter may not be supported by Tushare trade_cal. |
| PASS | stock_basic_exchange:BSE | stock_basic | BSE | 318 | `{"exchange": "BSE", "fields": "ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date", "list_status": "L"}` | `{}` | `{}` |  |

## Sample rows
### daily_basic:600519.SH
```json
[
  {
    "ts_code": "600519.SH",
    "trade_date": "20260605",
    "close": 1272.86,
    "turnover_rate": 0.2504,
    "turnover_rate_f": 0.5787,
    "volume_ratio": 0.64,
    "pe": 19.3292,
    "pe_ttm": 19.2369,
    "pb": 5.9396,
    "ps": 9.4243,
    "ps_ttm": 9.2432,
    "dv_ratio": 4.0644,
    "dv_ttm": 4.0644,
    "total_share": 125008.1601,
    "float_share": 125008.1601,
    "free_share": 54094.8978,
    "total_mv": 159117886.6649,
    "circ_mv": 159117886.6649
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20260604",
    "close": 1268.0,
    "turnover_rate": 0.268,
    "turnover_rate_f": 0.6194,
    "volume_ratio": 0.66,
    "pe": 19.2554,
    "pe_ttm": 19.1634,
    "pb": 5.9169,
    "ps": 9.3883,
    "ps_ttm": 9.2079,
    "dv_ratio": 4.08,
    "dv_ttm": 4.08,
    "total_share": 125008.1601,
    "float_share": 125008.1601,
    "free_share": 54094.8978,
    "total_mv": 158510347.0068,
    "circ_mv": 158510347.0068
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20260603",
    "close": 1281.91,
    "turnover_rate": 0.4198,
    "turnover_rate_f": 0.9701,
    "volume_ratio": 0.92,
    "pe": 19.4666,
    "pe_ttm": 19.3736,
    "pb": 5.9818,
    "ps": 9.4913,
    "ps_ttm": 9.3089,
    "dv_ratio": 4.0357,
    "dv_ttm": 4.0357,
    "total_share": 125008.1601,
    "float_share": 125008.1601,
    "free_share": 54094.8978,
    "total_mv": 160249210.5138,
    "circ_mv": 160249210.5138
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20260602",
    "close": 1307.22,
    "turnover_rate": 0.2909,
    "turnover_rate_f": 0.6722,
    "volume_ratio": 0.62,
    "pe": 19.851,
    "pe_ttm": 19.7561,
    "pb": 6.0999,
    "ps": 9.6787,
    "ps_ttm": 9.4927,
    "dv_ratio": 3.9576,
    "dv_ttm": 3.9576,
    "total_share": 125008.1601,
    "float_share": 125008.1601,
    "free_share": 54094.8978,
    "total_mv": 163413167.0459,
    "circ_mv": 163413167.0459
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20260601",
    "close": 1309.6,
    "turnover_rate": 0.3507,
    "turnover_rate_f": 0.8105,
    "volume_ratio": 0.74,
    "pe": 19.8871,
    "pe_ttm": 19.7921,
    "pb": 6.111,
    "ps": 9.6963,
    "ps_ttm": 9.51,
    "dv_ratio": 3.9504,
    "dv_ttm": 3.9504,
    "total_share": 125008.1601,
    "float_share": 125008.1601,
    "free_share": 54094.8978,
    "total_mv": 163710686.467,
    "circ_mv": 163710686.467
  }
]
```
### daily_basic:000001.SZ
```json
[
  {
    "ts_code": "000001.SZ",
    "trade_date": "20260605",
    "close": 10.98,
    "turnover_rate": 0.5171,
    "turnover_rate_f": 1.2297,
    "volume_ratio": 1.02,
    "pe": 4.9979,
    "pe_ttm": 4.9484,
    "pb": 0.4591,
    "ps": 1.6211,
    "ps_ttm": 1.602,
    "dv_ratio": 5.4464,
    "dv_ttm": 5.4464,
    "total_share": 1940591.8198,
    "float_share": 1940560.0653,
    "free_share": 816048.1215,
    "total_mv": 21307698.1814,
    "circ_mv": 21307349.517
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "20260604",
    "close": 10.82,
    "turnover_rate": 0.4479,
    "turnover_rate_f": 1.0652,
    "volume_ratio": 0.88,
    "pe": 4.9251,
    "pe_ttm": 4.8763,
    "pb": 0.4524,
    "ps": 1.5975,
    "ps_ttm": 1.5786,
    "dv_ratio": 5.5269,
    "dv_ttm": 5.5269,
    "total_share": 1940591.8198,
    "float_share": 1940560.0653,
    "free_share": 816048.1215,
    "total_mv": 20997203.4902,
    "circ_mv": 20996859.9065
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "20260603",
    "close": 10.99,
    "turnover_rate": 0.4253,
    "turnover_rate_f": 1.0113,
    "volume_ratio": 0.83,
    "pe": 5.0025,
    "pe_ttm": 4.9529,
    "pb": 0.4596,
    "ps": 1.6225,
    "ps_ttm": 1.6034,
    "dv_ratio": 5.4414,
    "dv_ttm": 5.4414,
    "total_share": 1940591.8198,
    "float_share": 1940560.0653,
    "free_share": 816048.1215,
    "total_mv": 21327104.0996,
    "circ_mv": 21326755.1176
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "20260602",
    "close": 11.08,
    "turnover_rate": 0.4563,
    "turnover_rate_f": 1.085,
    "volume_ratio": 0.9,
    "pe": 5.0435,
    "pe_ttm": 4.9934,
    "pb": 0.4633,
    "ps": 1.6358,
    "ps_ttm": 1.6166,
    "dv_ratio": 5.3972,
    "dv_ttm": 5.3972,
    "total_share": 1940591.8198,
    "float_share": 1940560.0653,
    "free_share": 816048.1215,
    "total_mv": 21501757.3634,
    "circ_mv": 21501405.5235
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "20260601",
    "close": 10.99,
    "turnover_rate": 0.4919,
    "turnover_rate_f": 1.1698,
    "volume_ratio": 1.03,
    "pe": 5.0025,
    "pe_ttm": 4.9529,
    "pb": 0.4596,
    "ps": 1.6225,
    "ps_ttm": 1.6034,
    "dv_ratio": 5.4414,
    "dv_ttm": 5.4414,
    "total_share": 1940591.8198,
    "float_share": 1940560.0653,
    "free_share": 816048.1215,
    "total_mv": 21327104.0996,
    "circ_mv": 21326755.1176
  }
]
```
### dividend_ts_code_only:600519.SH
```json
[
  {
    "ts_code": "600519.SH",
    "end_date": "20251231",
    "ann_date": "20260417",
    "div_proc": "预案",
    "stk_div": 0.0,
    "stk_bo_rate": null,
    "stk_co_rate": null,
    "cash_div": 0.0,
    "cash_div_tax": 28.02423,
    "record_date": null,
    "ex_date": null,
    "pay_date": null,
    "div_listdate": null,
    "imp_ann_date": null,
    "base_date": "20260603",
    "base_share": 125008.1601
  },
  {
    "ts_code": "600519.SH",
    "end_date": "20251231",
    "ann_date": "20260417",
    "div_proc": "预案",
    "stk_div": 0.0,
    "stk_bo_rate": null,
    "stk_co_rate": null,
    "cash_div": 0.0,
    "cash_div_tax": 27.993,
    "record_date": null,
    "ex_date": null,
    "pay_date": null,
    "div_listdate": null,
    "imp_ann_date": null,
    "base_date": "20260417",
    "base_share": 125147.6039
  },
  {
    "ts_code": "600519.SH",
    "end_date": "20250930",
    "ann_date": "20251106",
    "div_proc": "预案",
    "stk_div": 0.0,
    "stk_bo_rate": null,
    "stk_co_rate": null,
    "cash_div": 0.0,
    "cash_div_tax": 23.957,
    "record_date": null,
    "ex_date": null,
    "pay_date": null,
    "div_listdate": null,
    "imp_ann_date": null,
    "base_date": "20251106",
    "base_share": 125227.0215
  },
  {
    "ts_code": "600519.SH",
    "end_date": "20250930",
    "ann_date": "20251106",
    "div_proc": "股东大会通过",
    "stk_div": 0.0,
    "stk_bo_rate": null,
    "stk_co_rate": null,
    "cash_div": 0.0,
    "cash_div_tax": 23.957,
    "record_date": null,
    "ex_date": null,
    "pay_date": null,
    "div_listdate": null,
    "imp_ann_date": null,
    "base_date": "20251106",
    "base_share": 125227.0215
  },
  {
    "ts_code": "600519.SH",
    "end_date": "20250930",
    "ann_date": "20251106",
    "div_proc": "实施",
    "stk_div": 0.0,
    "stk_bo_rate": null,
    "stk_co_rate": null,
    "cash_div": 23.957,
    "cash_div_tax": 23.957,
    "record_date": "20251218",
    "ex_date": "20251219",
    "pay_date": "20251219",
    "div_listdate": null,
    "imp_ann_date": "20251211",
    "base_date": "20251211",
    "base_share": 125227.0215
  }
]
```
### dividend_ts_code_only:000001.SZ
```json
[
  {
    "ts_code": "000001.SZ",
    "end_date": "20251231",
    "ann_date": "20260321",
    "div_proc": "预案",
    "stk_div": 0.0,
    "stk_bo_rate": null,
    "stk_co_rate": null,
    "cash_div": 0.0,
    "cash_div_tax": 0.36,
    "record_date": null,
    "ex_date": null,
    "pay_date": null,
    "div_listdate": null,
    "imp_ann_date": null,
    "base_date": "20251231",
    "base_share": 1940591.8198
  },
  {
    "ts_code": "000001.SZ",
    "end_date": "20251231",
    "ann_date": "20260321",
    "div_proc": "股东大会通过",
    "stk_div": 0.0,
    "stk_bo_rate": null,
    "stk_co_rate": null,
    "cash_div": 0.0,
    "cash_div_tax": 0.36,
    "record_date": null,
    "ex_date": null,
    "pay_date": null,
    "div_listdate": null,
    "imp_ann_date": null,
    "base_date": "20251231",
    "base_share": 1940591.8198
  },
  {
    "ts_code": "000001.SZ",
    "end_date": "20251231",
    "ann_date": "20260321",
    "div_proc": "实施",
    "stk_div": 0.0,
    "stk_bo_rate": null,
    "stk_co_rate": null,
    "cash_div": 0.36,
    "cash_div_tax": 0.36,
    "record_date": "20260611",
    "ex_date": "20260612",
    "pay_date": "20260612",
    "div_listdate": null,
    "imp_ann_date": "20260605",
    "base_date": "20260605",
    "base_share": 1940591.8198
  },
  {
    "ts_code": "000001.SZ",
    "end_date": "20250630",
    "ann_date": "20250823",
    "div_proc": "预案",
    "stk_div": 0.0,
    "stk_bo_rate": null,
    "stk_co_rate": null,
    "cash_div": 0.0,
    "cash_div_tax": 0.236,
    "record_date": null,
    "ex_date": null,
    "pay_date": null,
    "div_listdate": null,
    "imp_ann_date": null,
    "base_date": "20250630",
    "base_share": 1940591.8198
  },
  {
    "ts_code": "000001.SZ",
    "end_date": "20250630",
    "ann_date": "20250823",
    "div_proc": "实施",
    "stk_div": 0.0,
    "stk_bo_rate": null,
    "stk_co_rate": null,
    "cash_div": 0.236,
    "cash_div_tax": 0.236,
    "record_date": "20251014",
    "ex_date": "20251015",
    "pay_date": "20251015",
    "div_listdate": null,
    "imp_ann_date": "20250930",
    "base_date": "20250930",
    "base_share": 1940591.8198
  }
]
```
### share_float_long_range:600519.SH
```json
[
  {
    "ts_code": "600519.SH",
    "ann_date": "20090519",
    "float_date": "20090525",
    "float_share": 488679034.0,
    "float_ratio": 51.7778,
    "holder_name": "中国贵州茅台酒厂有限责任公司",
    "share_type": "股权分置限售股份"
  },
  {
    "ts_code": "600519.SH",
    "ann_date": "20080520",
    "float_date": "20080526",
    "float_share": 47190000.0,
    "float_ratio": 5.0,
    "holder_name": "中国贵州茅台酒厂有限责任公司",
    "share_type": "股权分置限售股份"
  },
  {
    "ts_code": "600519.SH",
    "ann_date": "20070518",
    "float_date": "20070525",
    "float_share": 3348482.0,
    "float_ratio": 0.35,
    "holder_name": "中国食品发酵工业研究院",
    "share_type": "股权分置限售股份"
  },
  {
    "ts_code": "600519.SH",
    "ann_date": "20070518",
    "float_date": "20070525",
    "float_share": 5218214.0,
    "float_ratio": 0.55,
    "holder_name": "贵州宏益投资贸易中心",
    "share_type": "股权分置限售股份"
  },
  {
    "ts_code": "600519.SH",
    "ann_date": "20070518",
    "float_date": "20070525",
    "float_share": 3348482.0,
    "float_ratio": 0.35,
    "holder_name": "上海捷强烟草糖酒（集团）有限公司",
    "share_type": "股权分置限售股份"
  }
]
```
### share_float_ts_code_only:600519.SH
```json
[
  {
    "ts_code": "600519.SH",
    "ann_date": "20090519",
    "float_date": "20090525",
    "float_share": 488679034.0,
    "float_ratio": 51.7778,
    "holder_name": "中国贵州茅台酒厂有限责任公司",
    "share_type": "股权分置限售股份"
  },
  {
    "ts_code": "600519.SH",
    "ann_date": "20080520",
    "float_date": "20080526",
    "float_share": 47190000.0,
    "float_ratio": 5.0,
    "holder_name": "中国贵州茅台酒厂有限责任公司",
    "share_type": "股权分置限售股份"
  },
  {
    "ts_code": "600519.SH",
    "ann_date": "20070518",
    "float_date": "20070525",
    "float_share": 3348482.0,
    "float_ratio": 0.35,
    "holder_name": "中国食品发酵工业研究院",
    "share_type": "股权分置限售股份"
  },
  {
    "ts_code": "600519.SH",
    "ann_date": "20070518",
    "float_date": "20070525",
    "float_share": 5218214.0,
    "float_ratio": 0.55,
    "holder_name": "贵州宏益投资贸易中心",
    "share_type": "股权分置限售股份"
  },
  {
    "ts_code": "600519.SH",
    "ann_date": "20070518",
    "float_date": "20070525",
    "float_share": 3348482.0,
    "float_ratio": 0.35,
    "holder_name": "上海捷强烟草糖酒（集团）有限公司",
    "share_type": "股权分置限售股份"
  }
]
```
### share_float_long_range:000001.SZ
```json
[
  {
    "ts_code": "000001.SZ",
    "ann_date": "20150520",
    "float_date": "20180521",
    "float_share": 252247983.0,
    "float_ratio": 1.4691,
    "holder_name": "中国平安保险(集团)股份有限公司-集团本级-自有资金",
    "share_type": "定增股份"
  },
  {
    "ts_code": "000001.SZ",
    "ann_date": "20150520",
    "float_date": "20180521",
    "float_share": 252247983.0,
    "float_ratio": 1.4691,
    "holder_name": "中国平安保险(集团)股份有限公司-集团本级-自有资金",
    "share_type": "定增股份"
  },
  {
    "ts_code": "000001.SZ",
    "ann_date": "20140108",
    "float_date": "20170109",
    "float_share": 2286809264.0,
    "float_ratio": 13.3183,
    "holder_name": "中国平安保险(集团)股份有限公司-集团本级-自有资金",
    "share_type": "定增股份"
  },
  {
    "ts_code": "000001.SZ",
    "ann_date": "20140108",
    "float_date": "20170109",
    "float_share": 2286809264.0,
    "float_ratio": 13.3183,
    "holder_name": "中国平安保险(集团)股份有限公司-集团本级-自有资金",
    "share_type": "定增股份"
  },
  {
    "ts_code": "000001.SZ",
    "ann_date": "20150520",
    "float_date": "20160523",
    "float_share": 5385030.0,
    "float_ratio": 0.0376,
    "holder_name": "财通基金-工商银行-富春定增196号资产管理计划",
    "share_type": "定增股份"
  }
]
```
### share_float_ts_code_only:000001.SZ
```json
[
  {
    "ts_code": "000001.SZ",
    "ann_date": "20150520",
    "float_date": "20180521",
    "float_share": 252247983.0,
    "float_ratio": 1.4691,
    "holder_name": "中国平安保险(集团)股份有限公司-集团本级-自有资金",
    "share_type": "定增股份"
  },
  {
    "ts_code": "000001.SZ",
    "ann_date": "20150520",
    "float_date": "20180521",
    "float_share": 252247983.0,
    "float_ratio": 1.4691,
    "holder_name": "中国平安保险(集团)股份有限公司-集团本级-自有资金",
    "share_type": "定增股份"
  },
  {
    "ts_code": "000001.SZ",
    "ann_date": "20140108",
    "float_date": "20170109",
    "float_share": 2286809264.0,
    "float_ratio": 13.3183,
    "holder_name": "中国平安保险(集团)股份有限公司-集团本级-自有资金",
    "share_type": "定增股份"
  },
  {
    "ts_code": "000001.SZ",
    "ann_date": "20140108",
    "float_date": "20170109",
    "float_share": 2286809264.0,
    "float_ratio": 13.3183,
    "holder_name": "中国平安保险(集团)股份有限公司-集团本级-自有资金",
    "share_type": "定增股份"
  },
  {
    "ts_code": "000001.SZ",
    "ann_date": "20150520",
    "float_date": "20160523",
    "float_share": 5385030.0,
    "float_ratio": 0.0376,
    "holder_name": "财通基金-工商银行-富春定增196号资产管理计划",
    "share_type": "定增股份"
  }
]
```
### suspend_long_range:600519.SH
```json
[
  {
    "ts_code": "600519.SH",
    "trade_date": "20011205",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20020225",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20020417",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20020429",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20020523",
    "suspend_timing": null,
    "suspend_type": "S"
  }
]
```
### suspend_ts_code_only:600519.SH
```json
[
  {
    "ts_code": "600519.SH",
    "trade_date": "20011205",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20020225",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20020417",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20020429",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "600519.SH",
    "trade_date": "20020523",
    "suspend_timing": null,
    "suspend_type": "S"
  }
]
```
### suspend_long_range:000001.SZ
```json
[
  {
    "ts_code": "000001.SZ",
    "trade_date": "19990528",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "19990816",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "20000622",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "20010411",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "20010629",
    "suspend_timing": null,
    "suspend_type": "S"
  }
]
```
### suspend_ts_code_only:000001.SZ
```json
[
  {
    "ts_code": "000001.SZ",
    "trade_date": "19990528",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "19990816",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "20000622",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "20010411",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "000001.SZ",
    "trade_date": "20010629",
    "suspend_timing": null,
    "suspend_type": "S"
  }
]
```
### suspend_known_date_all_market
```json
[
  {
    "ts_code": "000029.SZ",
    "trade_date": "20200312",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "000502.SZ",
    "trade_date": "20200312",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "000977.SZ",
    "trade_date": "20200312",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "002450.SZ",
    "trade_date": "20200312",
    "suspend_timing": null,
    "suspend_type": "S"
  },
  {
    "ts_code": "300592.SZ",
    "trade_date": "20200312",
    "suspend_timing": null,
    "suspend_type": "S"
  }
]
```
### trade_cal:blank
```json
[
  {
    "exchange": "SSE",
    "cal_date": "20260607",
    "is_open": 0,
    "pretrade_date": "20260605"
  },
  {
    "exchange": "SSE",
    "cal_date": "20260606",
    "is_open": 0,
    "pretrade_date": "20260605"
  },
  {
    "exchange": "SSE",
    "cal_date": "20260605",
    "is_open": 1,
    "pretrade_date": "20260604"
  },
  {
    "exchange": "SSE",
    "cal_date": "20260604",
    "is_open": 1,
    "pretrade_date": "20260603"
  },
  {
    "exchange": "SSE",
    "cal_date": "20260603",
    "is_open": 1,
    "pretrade_date": "20260602"
  }
]
```
### trade_cal:SSE
```json
[
  {
    "exchange": "SSE",
    "cal_date": "20260607",
    "is_open": 0,
    "pretrade_date": "20260605"
  },
  {
    "exchange": "SSE",
    "cal_date": "20260606",
    "is_open": 0,
    "pretrade_date": "20260605"
  },
  {
    "exchange": "SSE",
    "cal_date": "20260605",
    "is_open": 1,
    "pretrade_date": "20260604"
  },
  {
    "exchange": "SSE",
    "cal_date": "20260604",
    "is_open": 1,
    "pretrade_date": "20260603"
  },
  {
    "exchange": "SSE",
    "cal_date": "20260603",
    "is_open": 1,
    "pretrade_date": "20260602"
  }
]
```
### trade_cal:SZSE
```json
[
  {
    "exchange": "SZSE",
    "cal_date": "20260607",
    "is_open": 0,
    "pretrade_date": "20260605"
  },
  {
    "exchange": "SZSE",
    "cal_date": "20260606",
    "is_open": 0,
    "pretrade_date": "20260605"
  },
  {
    "exchange": "SZSE",
    "cal_date": "20260605",
    "is_open": 1,
    "pretrade_date": "20260604"
  },
  {
    "exchange": "SZSE",
    "cal_date": "20260604",
    "is_open": 1,
    "pretrade_date": "20260603"
  },
  {
    "exchange": "SZSE",
    "cal_date": "20260603",
    "is_open": 1,
    "pretrade_date": "20260602"
  }
]
```
### stock_basic_exchange:BSE
```json
[
  {
    "ts_code": "920964.BJ",
    "symbol": "920964",
    "name": "润农节水",
    "area": "河北",
    "industry": "建筑工程",
    "market": "北交所",
    "exchange": "BSE",
    "list_status": "L",
    "list_date": "20200727",
    "delist_date": null
  },
  {
    "ts_code": "920418.BJ",
    "symbol": "920418",
    "name": "苏轴股份",
    "area": "江苏",
    "industry": "汽车配件",
    "market": "北交所",
    "exchange": "BSE",
    "list_status": "L",
    "list_date": "20200727",
    "delist_date": null
  },
  {
    "ts_code": "920198.BJ",
    "symbol": "920198",
    "name": "微创光电",
    "area": "湖北",
    "industry": "IT设备",
    "market": "北交所",
    "exchange": "BSE",
    "list_status": "L",
    "list_date": "20200727",
    "delist_date": null
  },
  {
    "ts_code": "920149.BJ",
    "symbol": "920149",
    "name": "旭杰科技",
    "area": "江苏",
    "industry": "建筑工程",
    "market": "北交所",
    "exchange": "BSE",
    "list_status": "L",
    "list_date": "20200727",
    "delist_date": null
  },
  {
    "ts_code": "920184.BJ",
    "symbol": "920184",
    "name": "国源科技",
    "area": "北京",
    "industry": "软件服务",
    "market": "北交所",
    "exchange": "BSE",
    "list_status": "L",
    "list_date": "20200727",
    "delist_date": null
  }
]
```