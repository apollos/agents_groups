from __future__ import annotations

import importlib.util
import os
from datetime import date
from typing import Any, Iterable

from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.normalization.ticker import normalize_ticker, to_tushare_symbol
from stock_data_ingestion.schemas.errors import ErrorCode
from stock_data_ingestion.schemas.records import AdapterFetchStatus, ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest


class TushareAdapter(BaseDataAdapter):
    provider_name = "tushare"
    source_site = "tushare"
    adapter_version = "0.1.0"

    # stock_basic input/output checked against Tushare Pro manual doc_id=25.
    _STOCK_BASIC_FIELDS = (
        "ts_code,symbol,name,area,industry,fullname,enname,cnspell,market,exchange,"
        "curr_type,list_status,list_date,delist_date,is_hs,act_name,act_ent_type"
    )
    _SECURITY_MASTER_LIST_STATUSES = ("L", "D", "P")

    # stock_company input/output checked against Tushare Pro manual doc_id=112.
    _STOCK_COMPANY_FIELDS = (
        "ts_code,com_name,com_id,exchange,chairman,manager,secretary,reg_capital,"
        "setup_date,province,city,introduction,website,email,office,employees,"
        "main_business,business_scope"
    )

    # trade_cal checked against Tushare Pro manual doc_id=26. BSE is not a native
    # trade_cal exchange in the manual, so this adapter derives BSE from SSE.
    _TRADE_CAL_FIELDS = "exchange,cal_date,is_open,pretrade_date"
    _BSE_CALENDAR_SOURCE_EXCHANGE = "SSE"
    _TUSHARE_NATIVE_TRADE_CAL_EXCHANGES = {"", "SSE", "SZSE", "CFFEX", "SHFE", "CZCE", "DCE", "INE"}

    # Historical/market endpoints checked against manual: daily/weekly/monthly,
    # adj_factor, daily_basic, stk_limit, suspend_d and moneyflow.
    _STK_LIMIT_FIELDS = "trade_date,ts_code,pre_close,up_limit,down_limit"
    _SUSPEND_FIELDS = "ts_code,trade_date,suspend_timing,suspend_type"
    _MONEY_FLOW_FIELDS = (
        "ts_code,trade_date,buy_sm_vol,buy_sm_amount,sell_sm_vol,sell_sm_amount,"
        "buy_md_vol,buy_md_amount,sell_md_vol,sell_md_amount,buy_lg_vol,buy_lg_amount,"
        "sell_lg_vol,sell_lg_amount,buy_elg_vol,buy_elg_amount,sell_elg_vol,sell_elg_amount,"
        "net_mf_vol,net_mf_amount"
    )

    # Financial endpoints checked against Tushare Pro manual: income, balancesheet,
    # cashflow and fina_indicator. The statement fields are the minimum required to
    # fill FinancialStatementRecord while preserving raw Tushare names.
    _INCOME_FIELDS = (
        "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,end_type,"
        "total_revenue,revenue,operate_profit,total_profit,n_income,n_income_attr_p,update_flag"
    )
    _BALANCE_FIELDS = (
        "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,end_type,total_share,"
        "total_assets,total_liab,total_hldr_eqy_exc_min_int,total_hldr_eqy_inc_min_int,total_liab_hldr_eqy,update_flag"
    )
    _CASHFLOW_FIELDS = (
        "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,end_type,net_profit,"
        "n_cashflow_act,free_cashflow,update_flag"
    )

    # Corporate action endpoints checked against manual: dividend, share_float,
    # repurchase. dividend intentionally does not receive start_date/end_date.
    _DIVIDEND_FIELDS = (
        "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,"
        "cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,"
        "imp_ann_date,base_date,base_share"
    )
    _DIVIDEND_DATE_FIELDS = (
        "ann_date",
        "record_date",
        "ex_date",
        "imp_ann_date",
        "pay_date",
        "div_listdate",
        "base_date",
        "end_date",
    )
    _SHARE_FLOAT_FIELDS = "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type"
    _REPURCHASE_FIELDS = "ts_code,ann_date,end_date,proc,exp_date,vol,amount,high_limit,low_limit"
    _DEFAULT_EVENT_START_DATE = "19000101"

    def __init__(self) -> None:
        super().__init__()
        self.token = os.getenv("TUSHARE_TOKEN")
        self._pro: Any | None = None

    def is_available(self) -> bool:
        return bool(self.token) and importlib.util.find_spec("tushare") is not None

    def authenticate(self) -> bool:
        if not self.token:
            return False
        if importlib.util.find_spec("tushare") is None:
            return False
        import tushare as ts  # type: ignore

        ts.set_token(self.token)
        self._pro = ts.pro_api(self.token)
        self._authenticated = True
        return True

    def _classify_error(self, exc: Exception) -> ErrorCode:
        msg = str(exc).lower()
        if "permission" in msg or "权限" in msg:
            return ErrorCode.PERMISSION_DENIED
        if "rate" in msg or "limit" in msg or "频次" in msg or "超过" in msg:
            return ErrorCode.RATE_LIMITED
        if "timeout" in msg or "timed out" in msg:
            return ErrorCode.PROVIDER_TIMEOUT
        return ErrorCode.UNKNOWN_ERROR

    @staticmethod
    def _dataframe_to_records(df: Any) -> list[dict[str, Any]]:
        if df is None:
            return []
        if bool(getattr(df, "empty", False)):
            return []
        return df.to_dict(orient="records")

    @staticmethod
    def _date_to_tushare(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, date):
            return value.strftime("%Y%m%d")
        text = str(value).strip().replace("-", "")
        return text or None

    def _request_start_end(self, request: StockDataRequest) -> tuple[str | None, str | None]:
        return self._date_to_tushare(request.start_date), self._date_to_tushare(request.end_date)

    def _event_start_end(self, request: StockDataRequest) -> tuple[str, str]:
        """Return event date range for sparse corporate-action/event endpoints.

        Market/valuation endpoints should use the caller supplied range. Company
        actions and sparse event endpoints should not silently inherit a short
        rolling window; when no range is supplied, default to all-history.
        """

        extra = request.extra_params or {}
        start = self._date_to_tushare(extra.get("event_start_date")) or self._date_to_tushare(request.start_date) or self._DEFAULT_EVENT_START_DATE
        end = self._date_to_tushare(extra.get("event_end_date")) or self._date_to_tushare(request.end_date) or now_asia_shanghai().date().strftime("%Y%m%d")
        return start, end

    def _trading_status_start_end(self, request: StockDataRequest) -> tuple[str | None, str | None]:
        """Trading status defaults to the requested window, or today when absent.

        Unlike dividend/share_float, stk_limit can be a very large daily table.
        A missing date window should mean "current status", not all-history.
        """

        start, end = self._request_start_end(request)
        if start is None and end is None:
            today = now_asia_shanghai().date().strftime("%Y%m%d")
            return today, today
        return start, end

    @staticmethod
    def _normalize_api_date(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip().replace("-", "")
        if len(text) >= 8 and text[:8].isdigit():
            return text[:8]
        return None

    def _row_date_in_range(
        self,
        row: dict[str, Any],
        *,
        start_date: str | None,
        end_date: str | None,
        date_fields: Iterable[str],
        match_any_date_field: bool = True,
    ) -> bool:
        if not start_date and not end_date:
            return True
        values = [self._normalize_api_date(row.get(field)) for field in date_fields]
        values = [value for value in values if value]
        if not values:
            return False

        def _inside(value: str) -> bool:
            return (start_date is None or value >= start_date) and (end_date is None or value <= end_date)

        return any(_inside(value) for value in values) if match_any_date_field else all(_inside(value) for value in values)

    def _event_empty_result(self, source_api: str, started_at: Any, message: str) -> ProviderFetchResult:
        """Represent a valid sparse-event query with no rows as non-error."""

        return ProviderFetchResult(
            provider=self.provider_name,
            source_api=source_api,
            source_site=self.source_site,
            adapter_version=self.adapter_version,
            status=AdapterFetchStatus.empty_result,
            raw_records=[],
            rows_fetched=0,
            started_at=started_at,
            completed_at=now_asia_shanghai(),
            error=None,
        )

    def _success_or_event_empty_result(self, source_api: str, records: list[dict[str, Any]], started_at: Any, empty_message: str) -> ProviderFetchResult:
        if records:
            return self._success_result(source_api, records, started_at)
        return self._event_empty_result(source_api, started_at, empty_message)

    def _ensure_ready(self, source_api: str, started_at: Any | None = None) -> ProviderFetchResult | None:
        if not self.token:
            return self._unavailable_result(source_api, ErrorCode.TOKEN_MISSING, "TUSHARE_TOKEN is missing")
        if not self.is_available() or not self.authenticate():
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "tushare SDK unavailable")
        return None

    # ------------------------------------------------------------------
    # Security master / calendar
    # ------------------------------------------------------------------
    def _fetch_stock_company_by_ts_code(self, ts_code: str) -> dict[str, Any] | None:
        """Fetch stock_company by ts_code and return a single provider row.

        stock_company is supplementary to stock_basic. A failure here should not
        make stock_basic unavailable, but the error is preserved on the row when
        possible by the caller.
        """

        df = self._pro.stock_company(ts_code=ts_code, fields=self._STOCK_COMPANY_FIELDS)
        rows = self._dataframe_to_records(df)
        return dict(rows[0]) if rows else None

    @staticmethod
    def _merge_stock_company_fields(stock_basic_row: dict[str, Any], company_row: dict[str, Any] | None) -> dict[str, Any]:
        if not company_row:
            return dict(stock_basic_row)
        merged = dict(stock_basic_row)
        merged["stock_company_raw"] = company_row
        merged["stock_company_source_api"] = "stock_company"
        # Keep both Tushare raw names and project-friendly aliases so downstream
        # normalizers can consume them without losing raw traceability.
        if company_row.get("com_name") and not merged.get("fullname"):
            merged["fullname"] = company_row.get("com_name")
        if company_row.get("com_name"):
            merged["company_full_name"] = company_row.get("com_name")
        if company_row.get("main_business"):
            merged["main_business"] = company_row.get("main_business")
        if company_row.get("province") and not merged.get("area"):
            merged["area"] = company_row.get("province")
        for key, value in company_row.items():
            merged.setdefault(f"stock_company_{key}", value)
        return merged

    def _fetch_stock_basic_rows_for_requested_tickers(
        self,
        fields: str,
        requested_tickers: set[str],
    ) -> list[dict[str, Any]]:
        """Fetch stock_basic rows by exact ``ts_code`` for targeted requests.

        Do not fetch the full L/D/P universe before filtering. Tushare's delisted
        universe contains historical non-six-digit codes such as ``TS0018.SH``.
        Those are valid raw Tushare history, but they are outside this project's
        normalized A-share ticker grammar and must not break normal ticker queries.
        """

        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for ticker in sorted(requested_tickers):
            ts_code = to_tushare_symbol(ticker)

            query_records = self._dataframe_to_records(self._pro.stock_basic(ts_code=ts_code, fields=fields))

            if not query_records:
                for status in (*self._SECURITY_MASTER_LIST_STATUSES, "G"):
                    query_records.extend(
                        self._dataframe_to_records(
                            self._pro.stock_basic(ts_code=ts_code, list_status=status, fields=fields)
                        )
                    )

            company_row: dict[str, Any] | None = None
            try:
                company_row = self._fetch_stock_company_by_ts_code(ts_code)
            except Exception as exc:  # noqa: BLE001
                company_row = {"stock_company_error": str(exc), "ts_code": ts_code}

            for row in query_records:
                raw_ts_code = row.get("ts_code") or row.get("symbol") or ts_code
                try:
                    normalized = normalize_ticker(raw_ts_code)
                except Exception:  # noqa: BLE001
                    continue

                if normalized not in requested_tickers:
                    continue

                dedupe_key = (normalized, str(row.get("list_status", "")))
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                rows.append(self._merge_stock_company_fields(dict(row), company_row))

        return rows

    def _fetch_full_security_master_rows(self, fields: str) -> list[dict[str, Any]]:
        """Fetch L/D/P security master rows without normalizing provider symbols."""

        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for status in self._SECURITY_MASTER_LIST_STATUSES:
            status_rows = self._dataframe_to_records(self._pro.stock_basic(exchange="", list_status=status, fields=fields))
            for row in status_rows:
                ts_code = str(row.get("ts_code") or "").strip()
                list_status = str(row.get("list_status") or status).strip()
                dedupe_key = (ts_code, list_status)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                rows.append(row)
        return rows

    def fetch_security_master(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_basic"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api, started)
        if unavailable is not None:
            return unavailable
        try:
            fields = self._STOCK_BASIC_FIELDS
            if request.tickers:
                raw_records = self._fetch_stock_basic_rows_for_requested_tickers(fields, set(request.tickers))
            else:
                raw_records = self._fetch_full_security_master_rows(fields)
            return self._success_result(source_api, raw_records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_trade_calendar(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "trade_cal"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api, started)
        if unavailable is not None:
            return unavailable
        try:
            start, end = self._request_start_end(request)
            exchanges = request.exchanges or ["SSE"]
            records: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for requested_exchange in exchanges:
                exchange = str(requested_exchange or "SSE").upper()
                source_exchange = exchange
                is_derived_bse = False
                if exchange == "BSE":
                    source_exchange = self._BSE_CALENDAR_SOURCE_EXCHANGE
                    is_derived_bse = True
                elif exchange not in self._TUSHARE_NATIVE_TRADE_CAL_EXCHANGES:
                    source_exchange = ""

                df = self._pro.trade_cal(exchange=source_exchange, start_date=start, end_date=end, fields=self._TRADE_CAL_FIELDS)
                for row in self._dataframe_to_records(df):
                    row = dict(row)
                    if is_derived_bse:
                        row = {
                            **row,
                            "exchange": "BSE",
                            "provider_exchange": source_exchange,
                            "calendar_derivation": "derived_from_sse_trade_calendar",
                            "derivation_reason": "tushare_trade_cal_does_not_support_bse_exchange_parameter",
                        }
                    else:
                        row = {**row, "exchange": row.get("exchange") or exchange}
                    key = (str(row.get("exchange") or ""), str(row.get("cal_date") or ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(row)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    # ------------------------------------------------------------------
    # Market data and trading status
    # ------------------------------------------------------------------
    def fetch_trading_status(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "trading_status"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api, started)
        if unavailable is not None:
            return unavailable
        try:
            start, end = self._trading_status_start_end(request)
            records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for ticker in request.tickers:
                ts_code = to_tushare_symbol(ticker)

                limit_df = self._pro.stk_limit(ts_code=ts_code, start_date=start, end_date=end, fields=self._STK_LIMIT_FIELDS)
                for row in self._dataframe_to_records(limit_df):
                    row = dict(row)
                    trade_date = str(row.get("trade_date") or "")
                    key = (ts_code, trade_date)
                    records_by_key[key] = {
                        **row,
                        "ts_code": ts_code,
                        "provider_symbol": ts_code,
                        "source_api": "stk_limit",
                        "is_suspended": False,
                        "is_trading": True,
                        "tradability_status": "tradable",
                        "limit_up_price": row.get("up_limit"),
                        "limit_down_price": row.get("down_limit"),
                        "event_absent": False,
                    }

                suspend_df = self._pro.suspend_d(ts_code=ts_code, start_date=start, end_date=end, fields=self._SUSPEND_FIELDS)
                for row in self._dataframe_to_records(suspend_df):
                    row = dict(row)
                    trade_date = str(row.get("trade_date") or "")
                    key = (ts_code, trade_date)
                    suspend_type = str(row.get("suspend_type") or "").upper()
                    base = records_by_key.get(key, {"ts_code": ts_code, "provider_symbol": ts_code, "trade_date": trade_date})
                    records_by_key[key] = {
                        **base,
                        **row,
                        "ts_code": ts_code,
                        "provider_symbol": ts_code,
                        "source_api": "suspend_d" if suspend_type == "S" else base.get("source_api", "suspend_d"),
                        "is_suspended": suspend_type == "S",
                        "is_trading": suspend_type != "S",
                        "tradability_status": "not_tradable" if suspend_type == "S" else "tradable",
                        "not_tradable_reason": "suspended" if suspend_type == "S" else None,
                        "suspend_reason": row.get("suspend_timing") or row.get("suspend_type"),
                        "event_absent": False,
                    }
            return self._success_or_event_empty_result(source_api, list(records_by_key.values()), started, "no trading status event in requested range")
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_historical_bars(self, request: StockDataRequest) -> ProviderFetchResult:
        frequency = request.frequency or "1d"
        source_api = {"1d": "daily", "1w": "weekly", "1mo": "monthly"}.get(str(frequency), "pro_bar")
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api, started)
        if unavailable is not None:
            return unavailable
        try:
            records: list[dict[str, Any]] = []
            import tushare as ts  # type: ignore

            for ticker in request.tickers:
                ts_code = to_tushare_symbol(ticker)
                start, end = self._request_start_end(request)
                if source_api in {"daily", "weekly", "monthly"} and str(request.adjust or "none") == "none":
                    df = getattr(self._pro, source_api)(ts_code=ts_code, start_date=start, end_date=end)
                else:
                    freq = {
                        "1d": "D",
                        "1w": "W",
                        "1mo": "M",
                        "1m": "1min",
                        "5m": "5min",
                        "15m": "15min",
                        "30m": "30min",
                        "60m": "60min",
                    }.get(str(frequency), "D")
                    adj = None if str(request.adjust or "none") == "none" else str(request.adjust)
                    df = ts.pro_bar(ts_code=ts_code, start_date=start, end_date=end, freq=freq, adj=adj)
                for row in self._dataframe_to_records(df):
                    row = dict(row)
                    row["provider_symbol"] = ts_code
                    records.append(row)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_adj_factor(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "adj_factor"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api, started)
        if unavailable is not None:
            return unavailable
        try:
            records: list[dict[str, Any]] = []
            start, end = self._request_start_end(request)
            for ticker in request.tickers:
                df = self._pro.adj_factor(ts_code=to_tushare_symbol(ticker), start_date=start, end_date=end)
                records.extend(self._dataframe_to_records(df))
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_valuation_metric(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "daily_basic"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api, started)
        if unavailable is not None:
            return unavailable
        try:
            records: list[dict[str, Any]] = []
            start, end = self._request_start_end(request)
            fields = (
                "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,"
                "pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv"
            )
            for ticker in request.tickers:
                df = self._pro.daily_basic(ts_code=to_tushare_symbol(ticker), start_date=start, end_date=end, fields=fields)
                records.extend(self._dataframe_to_records(df))
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_money_flow(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "moneyflow"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api, started)
        if unavailable is not None:
            return unavailable
        try:
            records: list[dict[str, Any]] = []
            start, end = self._request_start_end(request)
            for ticker in request.tickers:
                ts_code = to_tushare_symbol(ticker)
                df = self._pro.moneyflow(ts_code=ts_code, start_date=start, end_date=end, fields=self._MONEY_FLOW_FIELDS)
                for row in self._dataframe_to_records(df):
                    row = dict(row)
                    row["provider_symbol"] = ts_code
                    row["source_methodology"] = "tushare:moneyflow"
                    if row.get("buy_elg_amount") is not None or row.get("sell_elg_amount") is not None:
                        row["super_large_net_inflow"] = (row.get("buy_elg_amount") or 0) - (row.get("sell_elg_amount") or 0)
                    if row.get("buy_lg_amount") is not None or row.get("sell_lg_amount") is not None:
                        row["large_net_inflow"] = (row.get("buy_lg_amount") or 0) - (row.get("sell_lg_amount") or 0)
                    if row.get("buy_md_amount") is not None or row.get("sell_md_amount") is not None:
                        row["medium_net_inflow"] = (row.get("buy_md_amount") or 0) - (row.get("sell_md_amount") or 0)
                    if row.get("buy_sm_amount") is not None or row.get("sell_sm_amount") is not None:
                        row["small_net_inflow"] = (row.get("buy_sm_amount") or 0) - (row.get("sell_sm_amount") or 0)
                    row["main_net_inflow"] = row.get("net_mf_amount")
                    records.append(row)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    # ------------------------------------------------------------------
    # Financial statements / indicators
    # ------------------------------------------------------------------
    def _financial_statement_kwargs(self, request: StockDataRequest, ts_code: str, fields: str) -> dict[str, Any]:
        extra = request.extra_params or {}
        kwargs: dict[str, Any] = {"ts_code": ts_code, "fields": fields}
        if extra.get("period"):
            kwargs["period"] = self._date_to_tushare(extra.get("period"))
        else:
            start, end = self._request_start_end(request)
            if start:
                kwargs["start_date"] = start
            if end:
                kwargs["end_date"] = end
        for key in ("ann_date", "f_ann_date", "report_type", "comp_type"):
            if extra.get(key) is not None:
                kwargs[key] = str(extra[key])
        return kwargs

    @staticmethod
    def _decorate_income_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "source_api": "income",
            "statement_type": "income_statement",
            "operating_revenue": row.get("revenue") if row.get("revenue") is not None else row.get("total_revenue"),
            "operating_profit": row.get("operate_profit"),
            "net_profit": row.get("n_income"),
            "parent_net_profit": row.get("n_income_attr_p"),
        }

    @staticmethod
    def _decorate_balance_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "source_api": "balancesheet",
            "statement_type": "balance_sheet",
            "total_liabilities": row.get("total_liab"),
            "parent_equity": row.get("total_hldr_eqy_exc_min_int"),
        }

    @staticmethod
    def _decorate_cashflow_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "source_api": "cashflow",
            "statement_type": "cash_flow",
            "operating_cash_flow": row.get("n_cashflow_act"),
        }

    def fetch_financial_statement(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "financial_statement"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api, started)
        if unavailable is not None:
            return unavailable
        try:
            extra = request.extra_params or {}
            raw_types = extra.get("statement_types") or extra.get("financial_statement_types") or ["income", "balancesheet", "cashflow"]
            if isinstance(raw_types, str):
                statement_types = [part.strip().lower() for part in raw_types.split(",") if part.strip()]
            else:
                statement_types = [str(part).strip().lower() for part in raw_types if str(part).strip()]

            records: list[dict[str, Any]] = []
            for ticker in request.tickers:
                ts_code = to_tushare_symbol(ticker)
                if "income" in statement_types or "income_statement" in statement_types:
                    df = self._pro.income(**self._financial_statement_kwargs(request, ts_code, self._INCOME_FIELDS))
                    records.extend(self._decorate_income_row(dict(row)) for row in self._dataframe_to_records(df))
                if "balancesheet" in statement_types or "balance_sheet" in statement_types:
                    df = self._pro.balancesheet(**self._financial_statement_kwargs(request, ts_code, self._BALANCE_FIELDS))
                    records.extend(self._decorate_balance_row(dict(row)) for row in self._dataframe_to_records(df))
                if "cashflow" in statement_types or "cash_flow" in statement_types:
                    df = self._pro.cashflow(**self._financial_statement_kwargs(request, ts_code, self._CASHFLOW_FIELDS))
                    records.extend(self._decorate_cashflow_row(dict(row)) for row in self._dataframe_to_records(df))
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_financial_indicator(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "fina_indicator"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api, started)
        if unavailable is not None:
            return unavailable
        try:
            records: list[dict[str, Any]] = []
            start, end = self._request_start_end(request)
            period = self._date_to_tushare((request.extra_params or {}).get("period"))
            fields = "ts_code,ann_date,end_date,eps,bps,roe,roa,gross_margin,grossprofit_margin,netprofit_margin,or_yoy,netprofit_yoy,debt_to_assets,current_ratio,ocf_to_profit"
            for ticker in request.tickers:
                kwargs: dict[str, Any] = {"ts_code": to_tushare_symbol(ticker), "fields": fields}
                if period:
                    kwargs["period"] = period
                else:
                    kwargs["start_date"] = start
                    kwargs["end_date"] = end
                df = self._pro.fina_indicator(**kwargs)
                records.extend(self._dataframe_to_records(df))
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    # ------------------------------------------------------------------
    # Corporate actions
    # ------------------------------------------------------------------
    def _fetch_dividend_records(self, ts_code: str, start: str, end: str, event_date_field: str | None) -> list[dict[str, Any]]:
        # Tushare dividend does not accept start_date/end_date. Query by ts_code
        # and filter locally on the caller's event date semantics.
        df = self._pro.dividend(ts_code=ts_code, fields=self._DIVIDEND_FIELDS)
        rows = self._dataframe_to_records(df)
        date_fields = (event_date_field,) if event_date_field else self._DIVIDEND_DATE_FIELDS
        filtered: list[dict[str, Any]] = []
        for row in rows:
            row = dict(row)
            if not self._row_date_in_range(row, start_date=start, end_date=end, date_fields=date_fields):
                continue
            row = {
                **row,
                "provider_symbol": ts_code,
                "source_api": "dividend",
                "action_type": "dividend",
                "announcement_date": row.get("ann_date"),
                "record_date": row.get("record_date"),
                "ex_date": row.get("ex_date"),
                "cash_dividend_per_share": row.get("cash_div_tax") if row.get("cash_div_tax") is not None else row.get("cash_div"),
                "stock_bonus_ratio": row.get("stk_div") if row.get("stk_div") is not None else row.get("stk_bo_rate"),
                "dividend_payment_date": row.get("pay_date"),
                "event_date_filter_start": start,
                "event_date_filter_end": end,
                "event_date_filter_fields": ",".join(date_fields),
                "event_absent": False,
            }
            filtered.append(row)
        return filtered

    def _fetch_share_float_records(self, ts_code: str, start: str, end: str) -> list[dict[str, Any]]:
        df = self._pro.share_float(ts_code=ts_code, start_date=start, end_date=end, fields=self._SHARE_FLOAT_FIELDS)
        rows = self._dataframe_to_records(df)
        records: list[dict[str, Any]] = []
        for row in rows:
            row = dict(row)
            records.append(
                {
                    **row,
                    "provider_symbol": ts_code,
                    "source_api": "share_float",
                    "action_type": "share_float",
                    "announcement_date": row.get("ann_date"),
                    "ex_date": row.get("float_date"),
                    "event_date_filter_start": start,
                    "event_date_filter_end": end,
                    "event_absent": False,
                }
            )
        return records

    def _fetch_repurchase_records(self, requested_tickers: set[str], start: str, end: str) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"start_date": start, "end_date": end, "fields": self._REPURCHASE_FIELDS}
        df = self._pro.repurchase(**kwargs)
        records: list[dict[str, Any]] = []
        for row in self._dataframe_to_records(df):
            row = dict(row)
            ts_code = str(row.get("ts_code") or "")
            if requested_tickers and ts_code not in requested_tickers:
                continue
            if not self._row_date_in_range(row, start_date=start, end_date=end, date_fields=("ann_date", "end_date", "exp_date")):
                continue
            records.append(
                {
                    **row,
                    "provider_symbol": ts_code,
                    "source_api": "repurchase",
                    "action_type": "repurchase",
                    "announcement_date": row.get("ann_date"),
                    "event_date_filter_start": start,
                    "event_date_filter_end": end,
                    "event_absent": False,
                }
            )
        return records

    def fetch_corporate_action(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "corporate_action"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api, started)
        if unavailable is not None:
            return unavailable
        try:
            start, end = self._event_start_end(request)
            extra = request.extra_params or {}
            event_date_field = extra.get("event_date_field")
            action_types_raw = extra.get("action_types") or extra.get("corporate_action_types") or ["dividend", "share_float", "repurchase"]
            if isinstance(action_types_raw, str):
                action_types = [part.strip().lower() for part in action_types_raw.split(",") if part.strip()]
            else:
                action_types = [str(part).strip().lower() for part in action_types_raw if str(part).strip()]
            tickers = request.tickers or []
            requested_ts_codes = {to_tushare_symbol(ticker) for ticker in tickers}
            records: list[dict[str, Any]] = []
            for ticker in tickers:
                ts_code = to_tushare_symbol(ticker)
                if "dividend" in action_types:
                    records.extend(self._fetch_dividend_records(ts_code, start, end, str(event_date_field) if event_date_field else None))
                if "share_float" in action_types:
                    records.extend(self._fetch_share_float_records(ts_code, start, end))
            if "repurchase" in action_types:
                records.extend(self._fetch_repurchase_records(requested_ts_codes, start, end))
            records.sort(key=lambda row: (str(row.get("ts_code") or ""), str(row.get("announcement_date") or row.get("ann_date") or ""), str(row.get("source_api") or "")))
            return self._success_or_event_empty_result(source_api, records, started, "no corporate action event in requested range")
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def normalize_raw_data(self, result: ProviderFetchResult, request: StockDataRequest) -> list[Any]:
        return result.raw_records

    def map_provider_symbol_to_normalized_ticker(self, symbol: str) -> str:
        return normalize_ticker(symbol)

    def map_normalized_ticker_to_provider_symbol(self, ticker: str) -> str:
        return to_tushare_symbol(ticker)
