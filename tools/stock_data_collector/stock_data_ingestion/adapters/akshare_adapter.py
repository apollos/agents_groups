from __future__ import annotations

import importlib.util
import inspect
import math
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai, normalize_trade_date
from stock_data_ingestion.normalization.ticker import normalize_ticker, to_akshare_symbol
from stock_data_ingestion.schemas.errors import ErrorCode
from stock_data_ingestion.schemas.records import AdapterFetchStatus, ProviderFetchResult
from stock_data_ingestion.schemas.requests import Frequency, StockDataRequest


class AKShareAdapter(BaseDataAdapter):
    """AKShare adapter aligned with AKShare 1.18.x stock-data interfaces.

    The adapter intentionally returns raw-ish records with normalized aliases. The
    ingestion runner owns standard-record construction, but AKShare column names are
    often Chinese and source-specific, so this adapter adds the aliases required by
    the common normalizers while preserving original columns.
    """

    provider_name = "akshare"
    source_site = "akshare"
    adapter_version = "0.3.0"

    def is_available(self) -> bool:
        return importlib.util.find_spec("akshare") is not None

    def authenticate(self) -> bool:
        self._authenticated = self.is_available()
        return self._authenticated

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    def _import_ak(self, source_api: str, started: datetime):
        if not self.is_available():
            raise RuntimeError("akshare is not installed")
        import akshare as ak  # type: ignore

        return ak

    def _empty_result(self, source_api: str, raw_records: list[dict[str, Any]], started_at: datetime) -> ProviderFetchResult:
        """Return success for rows and empty_result without an ErrorRecord for sparse event APIs."""
        return ProviderFetchResult(
            provider=self.provider_name,
            source_api=source_api,
            source_site=self.source_site,
            adapter_version=self.adapter_version,
            status=AdapterFetchStatus.success if raw_records else AdapterFetchStatus.empty_result,
            raw_records=raw_records,
            rows_fetched=len(raw_records),
            started_at=started_at,
            completed_at=now_asia_shanghai(),
            error=None,
        )


    def _call_ak(self, func: Any, **kwargs: Any) -> Any:
        """Call an AKShare function while tolerating minor signature drift across versions."""
        try:
            return func(**kwargs)
        except TypeError as first_exc:
            try:
                sig = inspect.signature(func)
            except (TypeError, ValueError):
                raise first_exc
            accepted = {
                name
                for name, param in sig.parameters.items()
                if param.kind in {param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY}
            }
            filtered = {k: v for k, v in kwargs.items() if k in accepted}
            if filtered == kwargs or not filtered:
                raise first_exc
            return func(**filtered)

    def _records(self, df: Any) -> list[dict[str, Any]]:
        if df is None:
            return []
        if hasattr(df, "empty") and bool(getattr(df, "empty")):
            return []
        if hasattr(df, "to_dict"):
            records = df.to_dict(orient="records")
        elif isinstance(df, list):
            records = df
        else:
            return []
        return [self._clean_row(dict(row)) for row in records]

    def _clean_row(self, row: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in row.items():
            if self._is_missing(value):
                cleaned[str(key)] = None
            else:
                cleaned[str(key)] = self._json_safe(value)
        return cleaned

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        # pandas Timestamp / numpy scalar support without importing pandas/numpy.
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:  # noqa: BLE001
                pass
        return value

    def _is_missing(self, value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, float) and math.isnan(value):
            return True
        text = str(value).strip()
        return text in {"", "--", "-", "nan", "NaN", "None", "null", "NULL"}

    def _value(self, row: dict[str, Any], *keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in row and not self._is_missing(row[key]):
                return row[key]
        return default

    def _numeric(self, value: Any) -> float | None:
        if self._is_missing(value):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace(",", "").replace("％", "%")
        multiplier = 1.0
        if text.endswith("亿"):
            multiplier = 100_000_000.0
            text = text[:-1]
        elif text.endswith("万"):
            multiplier = 10_000.0
            text = text[:-1]
        elif text.endswith("千"):
            multiplier = 1_000.0
            text = text[:-1]
        if text.endswith("%"):
            text = text[:-1]
        try:
            return float(text) * multiplier
        except ValueError:
            match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
            return float(match.group(0)) * multiplier if match else None

    def _date_text(self, value: Any) -> str | None:
        if self._is_missing(value):
            return None
        if isinstance(value, datetime):
            return value.strftime("%Y%m%d")
        if isinstance(value, date):
            return value.strftime("%Y%m%d")
        text = str(value).strip()
        if re.fullmatch(r"\d{8}", text):
            return text
        text = text.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
        try:
            return normalize_trade_date(text).strftime("%Y%m%d")
        except Exception:  # noqa: BLE001
            digits = re.sub(r"\D", "", str(value))
            return digits[:8] if len(digits) >= 8 else None

    def _date_in_request_range(self, value: Any, request: StockDataRequest) -> bool:
        text = self._date_text(value)
        if not text:
            return True
        day = normalize_trade_date(text)
        if request.start_date and day < request.start_date:
            return False
        if request.end_date and day > request.end_date:
            return False
        return True

    def _datetime_text(self, value: Any) -> str | None:
        if self._is_missing(value):
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return datetime.combine(value, time.min).isoformat()
        text = str(value).strip().replace("/", "-")
        if re.fullmatch(r"\d{8}", text):
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}T00:00:00"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}$", text):
            return f"{text}T00:00:00"
        return text

    def _wanted_tickers(self, request: StockDataRequest) -> set[str]:
        return {normalize_ticker(t) for t in request.tickers}

    def _wanted_codes(self, request: StockDataRequest) -> set[str]:
        return {t.split(".")[0] for t in request.tickers}

    def _safe_normalize(self, code: Any, *, default_exchange: str | None = None) -> str | None:
        if self._is_missing(code):
            return None
        text = str(code).strip()
        if default_exchange and re.fullmatch(r"\d{6}", text):
            text = f"{text}.{default_exchange}"
        try:
            return normalize_ticker(text)
        except Exception:  # noqa: BLE001
            try:
                return normalize_ticker(re.sub(r"\D", "", text))
            except Exception:  # noqa: BLE001
                return None

    def _infer_exchange(self, ticker: str | None, row: dict[str, Any] | None = None) -> str | None:
        if ticker and "." in ticker:
            return ticker.split(".")[1]
        if row:
            market = str(self._value(row, "市场", "所属市场", "exchange", "交易所", default="")).upper()
            if "沪" in market or "SH" in market or "SSE" in market:
                return "SH"
            if "深" in market or "SZ" in market or "SZSE" in market:
                return "SZ"
            if "北" in market or "BJ" in market or "BSE" in market:
                return "BJ"
        return None

    def _market_code(self, ticker: str) -> str:
        return {"SH": "sh", "SZ": "sz", "BJ": "bj"}[ticker.split(".")[1]]

    def _em_symbol(self, ticker: str) -> str:
        code, exchange = normalize_ticker(ticker).split(".")
        return f"{exchange}{code}"

    def _ak_symbol(self, ticker: str) -> str:
        return to_akshare_symbol(ticker)[2:]

    def _add_identity(self, row: dict[str, Any], ticker: str | None = None) -> dict[str, Any]:
        inferred = ticker or self._safe_normalize(
            self._value(row, "normalized_ticker", "ts_code", "代码", "股票代码", "证券代码", "code", "symbol")
        )
        if inferred:
            row["normalized_ticker"] = inferred
            row.setdefault("provider_symbol", inferred.split(".")[0])
            row.setdefault("exchange", inferred.split(".")[1])
            row.setdefault("market", "A_share")
            row.setdefault("asset_type", "stock")
            row.setdefault("currency", "CNY")
        return row

    def _filter_stock_rows(self, rows: Iterable[dict[str, Any]], request: StockDataRequest) -> list[dict[str, Any]]:
        wanted = self._wanted_tickers(request)
        if not wanted:
            return list(rows)
        filtered: list[dict[str, Any]] = []
        for row in rows:
            ticker = self._safe_normalize(self._value(row, "normalized_ticker", "ts_code", "代码", "股票代码", "证券代码", "code", "symbol"))
            if ticker in wanted:
                filtered.append(row)
        return filtered

    def _period(self, request: StockDataRequest) -> str:
        return {"1d": "daily", "1w": "weekly", "1mo": "monthly"}.get(str(request.frequency or "1d"), "daily")

    def _minute_period(self, request: StockDataRequest) -> str:
        mapping = {
            Frequency.m1: "1",
            Frequency.m5: "5",
            Frequency.m15: "15",
            Frequency.m30: "30",
            Frequency.m60: "60",
            "1m": "1",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "60m": "60",
        }
        return mapping.get(request.frequency, mapping.get(str(request.frequency), "1"))

    def _adjust(self, request: StockDataRequest) -> str:
        return "" if str(request.adjust or "none") == "none" else str(request.adjust)

    def _start_date(self, request: StockDataRequest, default: str = "19700101") -> str:
        return request.start_date.strftime("%Y%m%d") if request.start_date else default

    def _end_date(self, request: StockDataRequest, default: str = "20991231") -> str:
        return request.end_date.strftime("%Y%m%d") if request.end_date else default

    def _date_range(self, request: StockDataRequest) -> list[date]:
        start = request.start_date or request.end_date or date.today()
        end = request.end_date or request.start_date or start
        days = (end - start).days
        if days < 0:
            return []
        max_days = int(request.extra_params.get("akshare_max_trading_status_days", 45))
        if days + 1 > max_days:
            start = end - timedelta(days=max_days - 1)
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def _first_existing_date(self, row: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = self._value(row, key)
            if value is not None:
                return value
        return None

    def _exchange_list_rows_by_code(self, ak: Any) -> dict[str, dict[str, Any]]:
        """Load exchange official stock-list rows keyed by six-digit stock code.

        These endpoints expose fields that stock_info_a_code_name does not, such as
        company_full_name/list_date/board/industry/share capital. They are optional
        enrichments; failures are swallowed so a transient exchange-list issue does
        not block code/name collection.
        """
        by_code: dict[str, dict[str, Any]] = {}

        def merge_rows(rows: list[dict[str, Any]], *, exchange: str, source_api: str) -> None:
            for row in rows:
                code = self._value(row, "证券代码", "A股代码", "code", "代码")
                if self._is_missing(code):
                    continue
                code_text = str(code).strip()
                if not re.fullmatch(r"\d{6}", code_text):
                    continue
                normalized = self._safe_normalize(code_text)
                if not normalized:
                    continue
                enriched = dict(row)
                enriched["exchange"] = exchange
                enriched["exchange_list_source_api"] = source_api
                by_code[code_text] = enriched

        try:
            # Shanghai main board and STAR board are separate in AKShare.
            for symbol in ["主板A股", "科创板"]:
                merge_rows(self._records(self._call_ak(ak.stock_info_sh_name_code, symbol=symbol)), exchange="SH", source_api="stock_info_sh_name_code")
        except Exception:  # noqa: BLE001
            pass
        try:
            merge_rows(self._records(self._call_ak(ak.stock_info_sz_name_code, symbol="A股列表")), exchange="SZ", source_api="stock_info_sz_name_code")
        except Exception:  # noqa: BLE001
            pass
        try:
            merge_rows(self._records(ak.stock_info_bj_name_code()), exchange="BJ", source_api="stock_info_bj_name_code")
        except Exception:  # noqa: BLE001
            pass
        return by_code

    def _merge_security_exchange_row(self, target: dict[str, Any], exchange_row: dict[str, Any] | None) -> dict[str, Any]:
        if not exchange_row:
            return target
        target["exchange_list_raw"] = exchange_row
        target["exchange_list_source_api"] = exchange_row.get("exchange_list_source_api")
        target["company_full_name"] = self._value(exchange_row, "公司全称") or target.get("company_full_name")
        target["list_date"] = self._date_text(self._value(exchange_row, "上市日期", "A股上市日期")) or target.get("list_date")
        target["board"] = self._value(exchange_row, "板块") or target.get("board")
        target["industry"] = self._value(exchange_row, "所属行业") or target.get("industry")
        target["area"] = self._value(exchange_row, "地区") or target.get("area")
        target["total_share"] = self._numeric(self._value(exchange_row, "总股本", "A股总股本")) or target.get("total_share")
        target["float_share"] = self._numeric(self._value(exchange_row, "流通股本", "A股流通股本")) or target.get("float_share")
        return target

    # ------------------------------------------------------------------
    # Fetch methods
    # ------------------------------------------------------------------
    def fetch_security_master(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_info_a_code_name+stock_individual_info_em+exchange_stock_lists"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            base_rows = self._records(ak.stock_info_a_code_name())
            if request.tickers:
                wanted_codes = self._wanted_codes(request)
                base_rows = [r for r in base_rows if str(self._value(r, "code", "代码")) in wanted_codes]

            exchange_rows = self._exchange_list_rows_by_code(ak)
            records: list[dict[str, Any]] = []
            for row in base_rows:
                code = str(self._value(row, "code", "代码", default="")).strip()
                ticker = self._safe_normalize(code)
                if not ticker:
                    continue
                enriched = dict(row)
                enriched["raw_source_api"] = "stock_info_a_code_name"
                enriched = self._add_identity(enriched, ticker)
                enriched.setdefault("name", self._value(row, "name", "名称", "股票简称"))
                enriched.setdefault("list_status", "L")
                enriched = self._merge_security_exchange_row(enriched, exchange_rows.get(code))

                try:
                    info_rows = self._records(self._call_ak(ak.stock_individual_info_em, symbol=code))
                    info = {str(self._value(i, "item", "项目", default="")).strip(): self._value(i, "value", "值") for i in info_rows}
                    enriched["raw_detail_source_api"] = "stock_individual_info_em"
                    enriched["raw_detail"] = info
                    enriched["industry"] = info.get("行业") or enriched.get("industry")
                    enriched["list_date"] = self._date_text(info.get("上市时间")) or enriched.get("list_date")
                    enriched["total_share"] = self._numeric(info.get("总股本")) or enriched.get("total_share")
                    enriched["float_share"] = self._numeric(info.get("流通股")) or self._numeric(info.get("流通股本")) or enriched.get("float_share")
                    enriched["total_market_value"] = self._numeric(info.get("总市值"))
                    enriched["float_market_value"] = self._numeric(info.get("流通市值"))
                    enriched["company_full_name"] = info.get("公司名称") or info.get("公司全称") or enriched.get("company_full_name")
                except Exception as exc:  # noqa: BLE001
                    # Detail endpoint is supplemental; keep exchange/code-name record and expose warning in raw row.
                    enriched["detail_warning"] = f"stock_individual_info_em failed: {exc}"

                records.append(enriched)
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except KeyError as exc:
            return self._error_result(source_api, started, exc, ErrorCode.PROVIDER_SCHEMA_CHANGED, retryable=False)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_trade_calendar(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "tool_trade_date_hist_sina"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            trade_rows = self._records(ak.tool_trade_date_hist_sina())
            trade_dates = sorted({normalize_trade_date(self._value(r, "trade_date", "日期")).strftime("%Y%m%d") for r in trade_rows if self._value(r, "trade_date", "日期")})
            if not trade_dates:
                return self._empty_result(source_api, [], started)
            start = request.start_date or normalize_trade_date(trade_dates[0])
            end = request.end_date or normalize_trade_date(trade_dates[-1])
            all_dates = [start + timedelta(days=i) for i in range((end - start).days + 1)] if end >= start else []
            trade_set = set(trade_dates)
            exchanges = request.exchanges or ["SSE", "SZSE", "BSE"]
            records: list[dict[str, Any]] = []
            for exchange in exchanges:
                for day in all_dates:
                    day_text = day.strftime("%Y%m%d")
                    prev_trade = max((d for d in trade_dates if d < day_text), default=None)
                    next_trade = min((d for d in trade_dates if d > day_text), default=None)
                    records.append(
                        {
                            "exchange": exchange,
                            "calendar_date": day_text,
                            "cal_date": day_text,
                            "is_open": day_text in trade_set,
                            "prev_trade_date": prev_trade,
                            "next_trade_date": next_trade,
                            "calendar_source": "sina_common_a_share_calendar",
                            "calendar_derivation": "common_a_share_calendar_used_for_exchange",
                        }
                    )
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_trading_status(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_tfp_em"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            records: list[dict[str, Any]] = []
            wanted = self._wanted_tickers(request)
            for day in self._date_range(request):
                date_text = day.strftime("%Y%m%d")
                rows = self._records(self._call_ak(ak.stock_tfp_em, date=date_text))
                suspended_by_ticker: dict[str, dict[str, Any]] = {}
                for raw in rows:
                    ticker = self._safe_normalize(self._value(raw, "代码", "股票代码", "证券代码"))
                    if not ticker:
                        continue
                    if wanted and ticker not in wanted:
                        continue
                    row = self._add_identity(dict(raw), ticker)
                    row.update(
                        {
                            "trade_date": date_text,
                            "is_suspended": True,
                            "is_trading": False,
                            "suspend_reason": self._value(raw, "停牌原因"),
                            "not_tradable_reason": self._value(raw, "停牌原因", default="suspended"),
                            "tradability_status": "suspended",
                            "raw_source_api": "stock_tfp_em",
                        }
                    )
                    suspended_by_ticker[ticker] = row
                    records.append(row)
                for ticker in wanted:
                    if ticker not in suspended_by_ticker:
                        records.append(
                            self._add_identity(
                                {
                                    "trade_date": date_text,
                                    "is_suspended": False,
                                    "is_trading": True,
                                    "tradability_status": "tradable_or_not_reported_suspended",
                                    "raw_source_api": "stock_tfp_em",
                                },
                                ticker,
                            )
                        )
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_historical_bars(self, request: StockDataRequest) -> ProviderFetchResult:
        is_minute = str(request.frequency or "1d") in {"1m", "5m", "15m", "30m", "60m"}
        source_api = "stock_zh_a_hist_min_em" if is_minute else "stock_zh_a_hist"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            records: list[dict[str, Any]] = []
            for ticker in request.tickers:
                normalized = normalize_ticker(ticker)
                symbol = self._ak_symbol(normalized)
                if is_minute:
                    start_date = (request.start_date or date.today()).strftime("%Y-%m-%d 09:30:00")
                    end_date = (request.end_date or date.today()).strftime("%Y-%m-%d 15:00:00")
                    df = self._call_ak(
                        ak.stock_zh_a_hist_min_em,
                        symbol=symbol,
                        start_date=start_date,
                        end_date=end_date,
                        period=self._minute_period(request),
                        adjust=self._adjust(request),
                    )
                    for row in self._records(df):
                        ts = self._datetime_text(self._value(row, "时间", "timestamp", "date", "日期"))
                        row["timestamp"] = ts
                        row["trade_date"] = self._date_text(ts)
                        row["provider_symbol"] = symbol
                        row["normalized_ticker"] = normalized
                        row["frequency"] = str(request.frequency or "1m")
                        row["adjust"] = str(request.adjust or "none")
                        records.append(row)
                else:
                    df = self._call_ak(
                        ak.stock_zh_a_hist,
                        symbol=symbol,
                        period=self._period(request),
                        start_date=self._start_date(request),
                        end_date=self._end_date(request),
                        adjust=self._adjust(request),
                    )
                    required = {"日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"}
                    columns = set(getattr(df, "columns", [])) if df is not None else set()
                    if df is not None and not required.issubset(columns):
                        raise KeyError(f"AKShare schema changed. Missing {required - columns}")
                    for row in self._records(df):
                        row["provider_symbol"] = symbol
                        row["normalized_ticker"] = normalized
                        row["trade_date"] = self._date_text(self._value(row, "日期"))
                        row["frequency"] = str(request.frequency or "1d")
                        row["adjust"] = str(request.adjust or "none")
                        records.append(row)
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except KeyError as exc:
            return self._error_result(source_api, started, exc, ErrorCode.PROVIDER_SCHEMA_CHANGED, retryable=False)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_realtime_quote(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_zh_a_spot_em"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            rows = self._records(ak.stock_zh_a_spot_em())
            if request.tickers:
                codes = self._wanted_codes(request)
                rows = [r for r in rows if str(self._value(r, "代码")) in codes]
            records: list[dict[str, Any]] = []
            for row in rows:
                ticker = self._safe_normalize(self._value(row, "代码"))
                if not ticker:
                    continue
                row = self._add_identity(row, ticker)
                row["latest_price"] = self._value(row, "最新价")
                row["pre_close"] = self._value(row, "昨收")
                row["pct_change"] = self._value(row, "涨跌幅")
                row["change"] = self._value(row, "涨跌额")
                row["volume"] = self._value(row, "成交量")
                row["amount"] = self._value(row, "成交额")
                row["total_market_value"] = self._value(row, "总市值")
                row["float_market_value"] = self._value(row, "流通市值")
                row["pe_ttm"] = self._value(row, "市盈率-动态")
                row["pb"] = self._value(row, "市净率")
                row["raw_source_api"] = source_api
                records.append(row)
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except KeyError as exc:
            return self._error_result(source_api, started, exc, ErrorCode.PROVIDER_SCHEMA_CHANGED, retryable=False)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_adj_factor(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result(
            "stock_zh_a_hist_adjusted_prices_only",
            ErrorCode.PROVIDER_UNAVAILABLE,
            "AKShare exposes qfq/hfq adjusted prices through stock_zh_a_hist, but this adapter has no standalone adj-factor table mapping.",
        )

    def fetch_financial_statement(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_profit_sheet_by_report_em+stock_balance_sheet_by_report_em+stock_cash_flow_sheet_by_report_em"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            statement_types = request.extra_params.get("statement_types") or ["income", "balancesheet", "cashflow"]
            records: list[dict[str, Any]] = []
            api_map = {
                "income": ("stock_profit_sheet_by_report_em", ak.stock_profit_sheet_by_report_em, "income_statement"),
                "profit": ("stock_profit_sheet_by_report_em", ak.stock_profit_sheet_by_report_em, "income_statement"),
                "balancesheet": ("stock_balance_sheet_by_report_em", ak.stock_balance_sheet_by_report_em, "balance_sheet"),
                "balance_sheet": ("stock_balance_sheet_by_report_em", ak.stock_balance_sheet_by_report_em, "balance_sheet"),
                "cashflow": ("stock_cash_flow_sheet_by_report_em", ak.stock_cash_flow_sheet_by_report_em, "cash_flow_statement"),
                "cash_flow": ("stock_cash_flow_sheet_by_report_em", ak.stock_cash_flow_sheet_by_report_em, "cash_flow_statement"),
            }
            for ticker in request.tickers:
                normalized = normalize_ticker(ticker)
                # EM financial-statement endpoints expect exchange-prefix format, e.g. SH600519.
                symbol = self._em_symbol(normalized)
                for key in statement_types:
                    if str(key) not in api_map:
                        continue
                    api_name, func, statement_type = api_map[str(key)]
                    for row in self._records(self._call_ak(func, symbol=symbol)):
                        report_date = self._first_existing_date(row, "REPORT_DATE", "报表日期", "日期", "报告期")
                        if report_date is not None and not self._date_in_request_range(report_date, request):
                            continue
                        row = self._add_identity(row, normalized)
                        row.update(
                            {
                                "statement_type": statement_type,
                                "raw_source_api": api_name,
                                "report_period": self._date_text(self._value(row, "REPORT_DATE", "报表日期", "报告期")) or self._value(row, "报告期"),
                                "report_date": self._date_text(self._value(row, "NOTICE_DATE", "公告日期", "REPORT_DATE", "报表日期")),
                                "announcement_date": self._date_text(self._value(row, "NOTICE_DATE", "公告日期")),
                                "operating_revenue": self._value(row, "OPERATE_INCOME", "营业总收入", "营业收入"),
                                "operating_profit": self._value(row, "OPERATE_PROFIT", "营业利润"),
                                "net_profit": self._value(row, "NETPROFIT", "净利润"),
                                "parent_net_profit": self._value(row, "PARENT_NETPROFIT", "归属于母公司股东的净利润", "归母净利润"),
                                "total_assets": self._value(row, "TOTAL_ASSETS", "资产总计", "总资产"),
                                "total_liabilities": self._value(row, "TOTAL_LIABILITIES", "负债合计", "总负债"),
                                "parent_equity": self._value(row, "TOTAL_EQUITY", "归属于母公司股东权益合计", "归母权益"),
                                "operating_cash_flow": self._value(row, "NETCASH_OPERATE", "经营活动产生的现金流量净额", "经营现金流"),
                            }
                        )
                        records.append(row)
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_financial_indicator(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_financial_analysis_indicator+stock_financial_analysis_indicator_em"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            records: list[dict[str, Any]] = []
            start_year = int(request.extra_params.get("start_year") or (request.start_date.year if request.start_date else date.today().year - 5))
            for ticker in request.tickers:
                normalized = normalize_ticker(ticker)
                symbol = self._ak_symbol(normalized)
                em_symbol = self._em_symbol(normalized)
                rows: list[dict[str, Any]] = []
                raw_api = "stock_financial_analysis_indicator"
                func = getattr(ak, "stock_financial_analysis_indicator", None)
                if func is not None:
                    try:
                        rows = self._records(self._call_ak(func, symbol=symbol, start_year=str(start_year)))
                    except Exception:  # noqa: BLE001
                        rows = []
                if not rows:
                    func = getattr(ak, "stock_financial_analysis_indicator_em", None)
                    if func is not None:
                        raw_api = "stock_financial_analysis_indicator_em"
                        # AKShare EM main-indicator endpoint expects suffix format such as 600519.SH.
                        try:
                            rows = self._records(self._call_ak(func, symbol=normalized, indicator="按报告期"))
                        except Exception:  # noqa: BLE001
                            rows = self._records(self._call_ak(func, symbol=normalized))
                for row in rows:
                    report_date = self._first_existing_date(row, "日期", "报告期", "report_period")
                    if report_date is not None and not self._date_in_request_range(report_date, request):
                        continue
                    row = self._add_identity(row, normalized)
                    row.update(
                        {
                            "raw_source_api": raw_api,
                            "report_period": self._date_text(self._value(row, "日期", "报告期")) or self._value(row, "日期", "报告期"),
                            "report_date": self._date_text(self._value(row, "日期", "报告期")),
                            "roe": self._value(row, "净资产收益率(%)", "加权净资产收益率(%)", "ROE"),
                            "roa": self._value(row, "总资产报酬率(%)", "总资产净利率(%)", "ROA"),
                            "gross_margin": self._value(row, "销售毛利率(%)", "毛利率"),
                            "net_margin": self._value(row, "销售净利率(%)", "净利率"),
                            "revenue_yoy": self._value(row, "主营业务收入增长率(%)", "营业收入同比增长率(%)", "营收同比"),
                            "net_profit_yoy": self._value(row, "净利润增长率(%)", "归属母公司股东的净利润增长率(%)", "净利润同比"),
                            "debt_asset_ratio": self._value(row, "资产负债率(%)", "资产负债率"),
                            "current_ratio": self._value(row, "流动比率", "current_ratio"),
                            "eps": self._value(row, "摊薄每股收益(元)", "每股收益", "EPS"),
                            "bps": self._value(row, "每股净资产_调整后(元)", "每股净资产", "BPS"),
                        }
                    )
                    records.append(row)
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_valuation_metric(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_value_em+stock_zh_a_spot_em"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            records: list[dict[str, Any]] = []
            for ticker in request.tickers:
                normalized = normalize_ticker(ticker)
                symbol = self._ak_symbol(normalized)
                ticker_rows: list[dict[str, Any]] = []
                value_api = getattr(ak, "stock_value_em", None)
                if value_api is not None:
                    try:
                        ticker_rows = self._records(self._call_ak(value_api, symbol=symbol))
                    except Exception:  # noqa: BLE001
                        ticker_rows = []
                if ticker_rows:
                    for row in ticker_rows:
                        if not self._date_in_request_range(self._value(row, "数据日期", "日期", "trade_date"), request):
                            continue
                        row = self._add_identity(row, normalized)
                        row.update(
                            {
                                "raw_source_api": "stock_value_em",
                                "trade_date": self._date_text(self._value(row, "数据日期", "日期")),
                                "close": self._value(row, "当日收盘价"),
                                "pct_change": self._value(row, "当日涨跌幅"),
                                "total_market_value": self._value(row, "总市值", "总市值(元)"),
                                "float_market_value": self._value(row, "流通市值", "流通市值(元)"),
                                "total_share": self._value(row, "总股本", "总股本(股)"),
                                "float_share": self._value(row, "流通股本", "流通股本(股)"),
                                "pe_ttm": self._value(row, "PE(TTM)"),
                                "pe": self._value(row, "PE(静)"),
                                "pb": self._value(row, "市净率"),
                                "ps": self._value(row, "市销率"),
                                "pcf": self._value(row, "市现率"),
                            }
                        )
                        records.append(row)
                    continue

                # Fallback: current Eastmoney A-share spot table exposes PE/PB/market-cap fields.
                for row in self._records(ak.stock_zh_a_spot_em()):
                    if str(self._value(row, "代码")) != symbol:
                        continue
                    row = self._add_identity(row, normalized)
                    row.update(
                        {
                            "raw_source_api": "stock_zh_a_spot_em",
                            "trade_date": (request.end_date or request.start_date or date.today()).strftime("%Y%m%d"),
                            "close": self._value(row, "最新价"),
                            "pct_change": self._value(row, "涨跌幅"),
                            "total_market_value": self._value(row, "总市值"),
                            "float_market_value": self._value(row, "流通市值"),
                            "pe_ttm": self._value(row, "市盈率-动态"),
                            "pe": self._value(row, "市盈率"),
                            "pb": self._value(row, "市净率"),
                            "turnover_rate": self._value(row, "换手率"),
                            "volume_ratio": self._value(row, "量比"),
                        }
                    )
                    records.append(row)
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_industry_membership(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_board_industry_cons_em+stock_board_concept_cons_em"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            wanted = self._wanted_tickers(request)
            include_industry = bool(request.extra_params.get("include_industry", True))
            include_concept = bool(request.extra_params.get("include_concept", True))
            max_boards = request.extra_params.get("akshare_max_boards")
            records: list[dict[str, Any]] = []

            if include_industry:
                industry_names = self._records(ak.stock_board_industry_name_em())
                for i, board in enumerate(industry_names):
                    if max_boards is not None and i >= int(max_boards):
                        break
                    board_name = self._value(board, "板块名称", "名称", "行业名称")
                    board_code = self._value(board, "板块代码", "代码")
                    if not board_name and not board_code:
                        continue
                    try:
                        cons = self._records(ak.stock_board_industry_cons_em(symbol=str(board_name or board_code)))
                    except Exception:  # noqa: BLE001
                        cons = self._records(ak.stock_board_industry_cons_em(symbol=str(board_code))) if board_code else []
                    for row in cons:
                        ticker = self._safe_normalize(self._value(row, "代码", "股票代码"))
                        if not ticker or (wanted and ticker not in wanted):
                            continue
                        row = self._add_identity(row, ticker)
                        row.update(
                            {
                                "industry_system": "akshare_eastmoney_board_industry",
                                "industry_code": board_code,
                                "industry_name": board_name,
                                "industry_level": 1,
                                "effective_date": self._start_date(request, date.today().strftime("%Y%m%d")),
                                "source_methodology": "stock_board_industry_name_em + stock_board_industry_cons_em",
                                "raw_source_api": "stock_board_industry_cons_em",
                            }
                        )
                        records.append(row)

            if include_concept:
                concept_names = self._records(ak.stock_board_concept_name_em())
                for i, board in enumerate(concept_names):
                    if max_boards is not None and i >= int(max_boards):
                        break
                    board_name = self._value(board, "板块名称", "名称", "概念名称")
                    board_code = self._value(board, "板块代码", "代码")
                    if not board_name and not board_code:
                        continue
                    try:
                        cons = self._records(ak.stock_board_concept_cons_em(symbol=str(board_name or board_code)))
                    except Exception:  # noqa: BLE001
                        cons = self._records(ak.stock_board_concept_cons_em(symbol=str(board_code))) if board_code else []
                    for row in cons:
                        ticker = self._safe_normalize(self._value(row, "代码", "股票代码"))
                        if not ticker or (wanted and ticker not in wanted):
                            continue
                        row = self._add_identity(row, ticker)
                        row.update(
                            {
                                "concept_code": board_code,
                                "concept_name": board_name,
                                "effective_date": self._start_date(request, date.today().strftime("%Y%m%d")),
                                "source_methodology": "stock_board_concept_name_em + stock_board_concept_cons_em",
                                "raw_source_api": "stock_board_concept_cons_em",
                            }
                        )
                        records.append(row)
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_concept_membership(self, request: StockDataRequest) -> ProviderFetchResult:
        request.extra_params["include_industry"] = False
        request.extra_params["include_concept"] = True
        return self.fetch_industry_membership(request)

    def fetch_money_flow(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_individual_fund_flow"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            records: list[dict[str, Any]] = []
            for ticker in request.tickers:
                normalized = normalize_ticker(ticker)
                symbol = self._ak_symbol(normalized)
                market = self._market_code(normalized)
                for row in self._records(self._call_ak(ak.stock_individual_fund_flow, stock=symbol, market=market)):
                    if not self._date_in_request_range(self._value(row, "日期", "trade_date"), request):
                        continue
                    row = self._add_identity(row, normalized)
                    row.update(
                        {
                            "trade_date": self._date_text(self._value(row, "日期")),
                            "frequency": "1d",
                            "source_methodology": "eastmoney_stock_individual_fund_flow_recent_100_trading_days",
                            "main_net_inflow": self._value(row, "主力净流入-净额", "主力净流入"),
                            "main_net_inflow_ratio": self._value(row, "主力净流入-净占比", "主力净流入占比"),
                            "super_large_net_inflow": self._value(row, "超大单净流入-净额", "超大单净流入"),
                            "large_net_inflow": self._value(row, "大单净流入-净额", "大单净流入"),
                            "medium_net_inflow": self._value(row, "中单净流入-净额", "中单净流入"),
                            "small_net_inflow": self._value(row, "小单净流入-净额", "小单净流入"),
                            "raw_source_api": source_api,
                        }
                    )
                    records.append(row)
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_index_data(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_zh_index_daily_em+index_stock_cons_weight_csindex"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            index_codes = request.extra_params.get("index_codes") or request.names or []
            if isinstance(index_codes, str):
                index_codes = [index_codes]
            include_bars = bool(request.extra_params.get("include_bars", True))
            include_constituents = bool(request.extra_params.get("include_constituents", True))
            records: list[dict[str, Any]] = []
            for index_code_raw in index_codes:
                index_code = str(index_code_raw).strip().upper().replace(".SH", "").replace(".SZ", "")
                if not index_code:
                    continue
                if include_bars:
                    symbol = index_code.lower()
                    if not symbol.startswith(("sh", "sz", "bj")):
                        symbol = ("sh" if index_code.startswith("000") else "sz") + index_code
                    for row in self._records(self._call_ak(ak.stock_zh_index_daily_em, symbol=symbol, start_date=self._start_date(request), end_date=self._end_date(request))):
                        row.update(
                            {
                                "index_code": index_code,
                                "trade_date": self._date_text(self._value(row, "date", "日期")),
                                "frequency": str(request.frequency or "1d"),
                                "currency": "CNY",
                                "market": "A_share",
                                "asset_type": "index",
                                "raw_source_api": "stock_zh_index_daily_em",
                            }
                        )
                        records.append(row)
                if include_constituents:
                    cons_rows: list[dict[str, Any]] = []
                    try:
                        cons_rows = self._records(ak.index_stock_cons_weight_csindex(symbol=index_code))
                    except Exception:  # noqa: BLE001
                        try:
                            cons_rows = self._records(ak.index_stock_cons_csindex(symbol=index_code))
                        except Exception:  # noqa: BLE001
                            cons_rows = []
                    for row in cons_rows:
                        ticker = self._safe_normalize(self._value(row, "成分券代码", "成分券代码Constituent Code", "品种代码", "代码", "stock_code"))
                        if not ticker:
                            continue
                        row = self._add_identity(row, ticker)
                        row.update(
                            {
                                "index_code": index_code,
                                "index_name": self._value(row, "指数名称", "index_name"),
                                "weight": self._value(row, "权重", "权重(%)", "weight"),
                                "effective_date": self._date_text(self._value(row, "日期", "date", default=self._start_date(request, date.today().strftime("%Y%m%d")))),
                                "raw_source_api": "index_stock_cons_weight_csindex",
                            }
                        )
                        records.append(row)
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_corporate_action(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_history_dividend_detail+stock_dividend_cninfo+stock_repurchase_em"
        started = now_asia_shanghai()
        try:
            ak = self._import_ak(source_api, started)
            action_types = request.extra_params.get("action_types") or ["dividend", "rights_issue", "repurchase"]
            records: list[dict[str, Any]] = []
            for ticker in request.tickers:
                normalized = normalize_ticker(ticker)
                symbol = self._ak_symbol(normalized)
                if "dividend" in action_types:
                    for row in self._records(self._call_ak(ak.stock_history_dividend_detail, symbol=symbol, indicator="分红")):
                        action_date = self._first_existing_date(row, "公告日期", "除权除息日", "股权登记日", "红股上市日")
                        if not self._date_in_request_range(action_date, request):
                            continue
                        row = self._add_identity(row, normalized)
                        row.update(
                            {
                                "action_type": "dividend",
                                "announcement_date": self._date_text(self._value(row, "公告日期")),
                                "record_date": self._date_text(self._value(row, "股权登记日")),
                                "ex_date": self._date_text(self._value(row, "除权除息日")),
                                "cash_dividend_per_share": (self._numeric(self._value(row, "派息")) / 10.0) if self._numeric(self._value(row, "派息")) is not None else None,
                                "stock_bonus_ratio": (self._numeric(self._value(row, "送股")) / 10.0) if self._numeric(self._value(row, "送股")) is not None else None,
                                "raw_source_api": "stock_history_dividend_detail",
                            }
                        )
                        records.append(row)
                    try:
                        for row in self._records(self._call_ak(ak.stock_dividend_cninfo, symbol=symbol)):
                            action_date = self._first_existing_date(row, "实施方案公告日期", "除权日", "股权登记日", "派息日", "报告时间")
                            if not self._date_in_request_range(action_date, request):
                                continue
                            row = self._add_identity(row, normalized)
                            row.update(
                                {
                                    "action_type": "dividend",
                                    "announcement_date": self._date_text(self._value(row, "实施方案公告日期")),
                                    "record_date": self._date_text(self._value(row, "股权登记日")),
                                    "ex_date": self._date_text(self._value(row, "除权日")),
                                    "dividend_payment_date": self._date_text(self._value(row, "派息日")),
                                    "cash_dividend_per_share": (self._numeric(self._value(row, "派息比例")) / 10.0) if self._numeric(self._value(row, "派息比例")) is not None else None,
                                    "stock_bonus_ratio": (self._numeric(self._value(row, "送股比例")) / 10.0) if self._numeric(self._value(row, "送股比例")) is not None else None,
                                    "raw_source_api": "stock_dividend_cninfo",
                                }
                            )
                            records.append(row)
                    except Exception as exc:  # noqa: BLE001
                        records.append(self._add_identity({"action_type": "dividend", "warning": f"stock_dividend_cninfo failed: {exc}"}, normalized))
                if "rights_issue" in action_types:
                    for row in self._records(self._call_ak(ak.stock_history_dividend_detail, symbol=symbol, indicator="配股")):
                        action_date = self._first_existing_date(row, "公告日期", "除权除息日", "股权登记日")
                        if not self._date_in_request_range(action_date, request):
                            continue
                        row = self._add_identity(row, normalized)
                        row.update(
                            {
                                "action_type": "rights_issue",
                                "announcement_date": self._date_text(self._value(row, "公告日期")),
                                "record_date": self._date_text(self._value(row, "股权登记日")),
                                "ex_date": self._date_text(self._value(row, "除权除息日")),
                                "rights_issue_ratio": (self._numeric(self._value(row, "配股")) / 10.0) if self._numeric(self._value(row, "配股")) is not None else None,
                                "rights_issue_price": self._value(row, "配股价", "配股价格"),
                                "raw_source_api": "stock_history_dividend_detail",
                            }
                        )
                        records.append(row)

            if "repurchase" in action_types:
                repurchase_rows = self._records(ak.stock_repurchase_em())
                for row in repurchase_rows:
                    ticker = self._safe_normalize(self._value(row, "股票代码", "代码"))
                    if not ticker or (request.tickers and ticker not in self._wanted_tickers(request)):
                        continue
                    action_date = self._first_existing_date(row, "最新公告日期", "回购起始时间", "公告日期")
                    if not self._date_in_request_range(action_date, request):
                        continue
                    row = self._add_identity(row, ticker)
                    row.update(
                        {
                            "action_type": "repurchase",
                            "announcement_date": self._date_text(self._value(row, "最新公告日期", "公告日期")),
                            "raw_source_api": "stock_repurchase_em",
                        }
                    )
                    records.append(row)
            return self._empty_result(source_api, records, started)
        except RuntimeError as exc:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def normalize_raw_data(self, result: ProviderFetchResult, request: StockDataRequest) -> list[Any]:
        return result.raw_records

    def map_provider_symbol_to_normalized_ticker(self, symbol: str) -> str:
        return normalize_ticker(symbol)

    def map_normalized_ticker_to_provider_symbol(self, ticker: str) -> str:
        return to_akshare_symbol(ticker)
