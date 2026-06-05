from __future__ import annotations

TUSHARE_DAILY_TO_STANDARD = {
    "ts_code": "normalized_ticker",
    "trade_date": "trade_date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "pre_close": "pre_close",
    "change": "change",
    "pct_chg": "pct_change",
    "vol": "volume",
    "amount": "amount",
}

AKSHARE_HIST_TO_STANDARD = {
    "日期": "trade_date",
    "股票代码": "normalized_ticker",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "涨跌幅": "pct_change",
    "涨跌额": "change",
    "换手率": "turnover_rate",
}

JOINQUANT_BAR_TO_STANDARD = {
    "time": "timestamp",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "money": "amount",
}
