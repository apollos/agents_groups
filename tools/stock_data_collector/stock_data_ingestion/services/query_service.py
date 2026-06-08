from __future__ import annotations

from datetime import date
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from stock_data_ingestion.normalization.datetime_utils import normalize_trade_date
from stock_data_ingestion.normalization.ticker import normalize_ticker
from stock_data_ingestion.storage import models


class QueryService:
    def __init__(self, session: Session, minimum_quality_for_trading_use: float = 0.80) -> None:
        self.session = session
        self.minimum_quality_for_trading_use = minimum_quality_for_trading_use

    def _rows_to_df(self, rows: Iterable[Any]) -> pd.DataFrame:
        result: list[dict[str, Any]] = []
        for row in rows:
            obj = row[0] if isinstance(row, tuple) else row
            result.append({col.name: getattr(obj, col.name) for col in obj.__table__.columns})
        return pd.DataFrame(result)

    def get_security(self, ticker: str) -> pd.DataFrame:
        return self.get_securities([ticker])

    def get_securities(self, tickers: list[str]) -> pd.DataFrame:
        normalized = [normalize_ticker(t) for t in tickers]
        stmt = select(models.SecurityModel).where(models.SecurityModel.normalized_ticker.in_(normalized))
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

    def _bar_statement(
        self,
        *,
        ticker: str,
        start_date: str | date,
        end_date: str | date,
        frequency: str,
        adjust: str,
        trading_ready: bool = False,
        minimum_quality: float | None = None,
    ):
        model_cls = models.BAR_MODEL_BY_FREQUENCY.get(frequency)
        if model_cls is None:
            raise ValueError(f"INVALID_REQUEST: unsupported frequency {frequency}")
        normalized = normalize_ticker(ticker)
        start = normalize_trade_date(start_date)
        end = normalize_trade_date(end_date)
        conditions = [
            model_cls.normalized_ticker == normalized,
            model_cls.trade_date >= start,
            model_cls.trade_date <= end,
            model_cls.frequency == frequency,
            model_cls.adjust == adjust,
        ]
        if trading_ready:
            threshold = self.minimum_quality_for_trading_use if minimum_quality is None else minimum_quality
            conditions.append(model_cls.validation_status != "quarantined")
            conditions.append(model_cls.data_quality >= threshold)
        return select(model_cls).where(*conditions).order_by(model_cls.trade_date, model_cls.timestamp)

    def _query_bars_df(
        self,
        ticker: str,
        start_date: str | date,
        end_date: str | date,
        frequency: str,
        adjust: str,
        *,
        trading_ready: bool = False,
        minimum_quality: float | None = None,
    ) -> pd.DataFrame:
        stmt = self._bar_statement(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjust=adjust,
            trading_ready=trading_ready,
            minimum_quality=minimum_quality,
        )
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

    def get_bars(self, ticker: str, start_date: str | date, end_date: str | date, frequency: str = "1d", adjust: str = "none") -> pd.DataFrame:
        adjust = str(adjust or "none")
        df = self._query_bars_df(ticker, start_date, end_date, frequency, adjust)
        if not df.empty or adjust == "none":
            return df
        raw = self._query_bars_df(ticker, start_date, end_date, frequency, "none")
        return self._apply_adjustment(raw, ticker, adjust)

    def get_trading_ready_bars(
        self,
        ticker: str,
        start_date: str | date,
        end_date: str | date,
        frequency: str = "1d",
        adjust: str = "qfq",
        minimum_quality: float | None = None,
    ) -> pd.DataFrame:
        """Return bars safe for factor/backtest use.

        Quarantined rows are excluded and data_quality must meet the configured
        trading threshold. If qfq/hfq rows are not explicitly stored, the method
        calculates an adjusted view from raw bars plus stored adjustment factors.
        """

        adjust = str(adjust or "none")
        df = self._query_bars_df(
            ticker,
            start_date,
            end_date,
            frequency,
            adjust,
            trading_ready=True,
            minimum_quality=minimum_quality,
        )
        if not df.empty or adjust == "none":
            return df
        raw = self._query_bars_df(
            ticker,
            start_date,
            end_date,
            frequency,
            "none",
            trading_ready=True,
            minimum_quality=minimum_quality,
        )
        return self._apply_adjustment(raw, ticker, adjust)

    def get_trading_ready_daily_bars(
        self,
        ticker: str,
        start_date: str | date,
        end_date: str | date,
        adjust: str = "qfq",
        minimum_quality: float | None = None,
    ) -> pd.DataFrame:
        return self.get_trading_ready_bars(ticker, start_date, end_date, frequency="1d", adjust=adjust, minimum_quality=minimum_quality)

    def _adjustment_factors(self, ticker: str) -> pd.DataFrame:
        normalized = normalize_ticker(ticker)
        stmt = (
            select(models.AdjFactorModel)
            .where(models.AdjFactorModel.normalized_ticker == normalized)
            .order_by(models.AdjFactorModel.effective_provider, models.AdjFactorModel.trade_date)
        )
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

    @staticmethod
    def _first_non_null(row: pd.Series, names: list[str]) -> float | None:
        for name in names:
            if name in row and pd.notna(row[name]):
                try:
                    return float(row[name])
                except (TypeError, ValueError):
                    return None
        return None

    def _apply_adjustment(self, bars: pd.DataFrame, ticker: str, adjust: str) -> pd.DataFrame:
        if bars.empty or adjust not in {"qfq", "hfq"}:
            return bars
        factors = self._adjustment_factors(ticker)
        if factors.empty:
            out = bars.copy()
            out["adjust"] = adjust
            out["adjustment_source"] = "no_adj_factor_available_raw_prices_returned"
            return out

        out_frames: list[pd.DataFrame] = []
        bars = bars.copy()
        bars["trade_date"] = pd.to_datetime(bars["trade_date"])
        factors = factors.copy()
        factors["trade_date"] = pd.to_datetime(factors["trade_date"])
        providers = list(bars["effective_provider"].dropna().unique()) if "effective_provider" in bars else [None]
        for provider in providers:
            b = bars if provider is None else bars[bars["effective_provider"] == provider]
            f = factors
            if provider is not None and "effective_provider" in factors.columns:
                same = factors[factors["effective_provider"] == provider]
                if not same.empty:
                    f = same
            f = f.sort_values("trade_date")
            b = b.sort_values("trade_date")
            merged = pd.merge_asof(b, f, on="trade_date", direction="backward", suffixes=("", "_factor"))
            factor_presence_cols = [
                col
                for col in ["adj_factor_factor", "adj_factor", "fore_adjust_factor", "back_adjust_factor"]
                if col in merged.columns
            ]
            if not factor_presence_cols or merged[factor_presence_cols].isna().all(axis=None):
                merged = pd.merge_asof(b, f, on="trade_date", direction="forward", suffixes=("", "_factor"))

            if adjust == "qfq":
                adj_series = merged.apply(lambda row: self._first_non_null(row, ["adj_factor_factor", "adj_factor"]), axis=1)
                fore_series = merged.apply(lambda row: self._first_non_null(row, ["fore_adjust_factor"]), axis=1)
                factor_provider = merged.get("effective_provider_factor")
                factor_method = merged.get("factor_method")
                is_baostock_factor = False
                if factor_provider is not None:
                    is_baostock_factor = factor_provider.astype(str).str.lower().eq("baostock").any()
                if factor_method is not None:
                    is_baostock_factor = is_baostock_factor or factor_method.astype(str).str.contains("baostock", case=False, na=False).any()

                # Tushare adj_factor is a cumulative daily factor and needs
                # current-period normalization. BaoStock foreAdjustFactor is
                # already the documented forward-adjustment multiplier, so use
                # it directly to avoid double-normalizing partial date ranges.
                if fore_series.notna().any() and (is_baostock_factor or adj_series.dropna().empty):
                    applied = fore_series
                else:
                    latest = adj_series.dropna().iloc[-1] if not adj_series.dropna().empty else None
                    applied = adj_series if latest in (None, 0) else adj_series / float(latest)
            else:
                factor_col_candidates = ["back_adjust_factor", "adj_factor_factor", "adj_factor"]
                applied = merged.apply(lambda row: self._first_non_null(row, factor_col_candidates), axis=1)
            applied = applied.fillna(1.0).astype(float)
            for col in ["open", "high", "low", "close", "pre_close"]:
                if col in merged.columns:
                    merged[f"raw_{col}"] = merged[col]
                    merged[col] = merged[col].astype(float) * applied
            merged["applied_adjust_factor"] = applied
            merged["adjust"] = adjust
            merged["adjustment_source"] = "calculated_from_raw_bars_and_adj_factor"
            out_frames.append(merged)
        if not out_frames:
            return bars
        return pd.concat(out_frames, ignore_index=True).sort_values(["trade_date", "timestamp"]).reset_index(drop=True)

    def get_valuation_metric(self, ticker: str, trade_date: str | date) -> pd.DataFrame:
        normalized = normalize_ticker(ticker)
        day = normalize_trade_date(trade_date)
        stmt = select(models.ValuationMetricModel).where(
            models.ValuationMetricModel.normalized_ticker == normalized,
            models.ValuationMetricModel.trade_date == day,
        )
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

    def get_financial_indicator(self, ticker: str, report_period: str) -> pd.DataFrame:
        normalized = normalize_ticker(ticker)
        stmt = select(models.FinancialIndicatorModel).where(
            models.FinancialIndicatorModel.normalized_ticker == normalized,
            models.FinancialIndicatorModel.report_period == report_period,
        )
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

    def get_industry_membership(self, ticker: str) -> pd.DataFrame:
        normalized = normalize_ticker(ticker)
        stmt = select(models.IndustryMembershipModel).where(models.IndustryMembershipModel.normalized_ticker == normalized)
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

    def get_concept_membership(self, ticker: str) -> pd.DataFrame:
        normalized = normalize_ticker(ticker)
        stmt = select(models.ConceptMembershipModel).where(models.ConceptMembershipModel.normalized_ticker == normalized)
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

    def get_money_flow(self, ticker: str, start_date: str | date, end_date: str | date) -> pd.DataFrame:
        normalized = normalize_ticker(ticker)
        start = normalize_trade_date(start_date)
        end = normalize_trade_date(end_date)
        stmt = select(models.MoneyFlowModel).where(
            models.MoneyFlowModel.normalized_ticker == normalized,
            models.MoneyFlowModel.trade_date >= start,
            models.MoneyFlowModel.trade_date <= end,
        )
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

    def get_index_constituents(self, index_code: str) -> pd.DataFrame:
        stmt = select(models.IndexConstituentModel).where(models.IndexConstituentModel.index_code == index_code)
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

    def get_raw_payload_index(self, raw_payload_id: str) -> pd.DataFrame:
        stmt = select(models.RawPayloadIndexModel).where(models.RawPayloadIndexModel.raw_payload_id == raw_payload_id)
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

    def get_raw_ref_by_record_id(self, record_id: str) -> dict[str, Any] | None:
        for model_cls in [
            models.SecurityModel,
            models.TradeCalendarModel,
            models.TradingStatusModel,
            models.DailyBarModel,
            models.WeeklyBarModel,
            models.MinuteBarModel,
            models.RealtimeQuoteModel,
            models.AdjFactorModel,
            models.FinancialStatementModel,
            models.FinancialIndicatorModel,
            models.ValuationMetricModel,
            models.IndustryMembershipModel,
            models.ConceptMembershipModel,
            models.MoneyFlowModel,
            models.IndexModel,
            models.IndexBarModel,
            models.IndexConstituentModel,
            models.CorporateActionModel,
        ]:
            obj = self.session.execute(select(model_cls).where(model_cls.record_id == record_id)).scalar_one_or_none()
            if obj is not None:
                return {
                    "record_id": record_id,
                    "raw_payload_id": obj.raw_payload_id,
                    "raw_payload_ref": obj.raw_payload_ref,
                    "raw_row_index": obj.raw_row_index,
                }
        return None

    def get_conflicts(self, ticker: str | None = None) -> pd.DataFrame:
        stmt = select(models.DataQualityConflictModel)
        if ticker:
            normalized = normalize_ticker(ticker)
            stmt = stmt.where(models.DataQualityConflictModel.comparison_key.contains(normalized))
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

    def is_quarantined(self, record_id: str) -> bool:
        for model_cls in [models.DailyBarModel, models.WeeklyBarModel, models.MinuteBarModel, models.TradingStatusModel]:
            obj = self.session.execute(select(model_cls).where(model_cls.record_id == record_id)).scalar_one_or_none()
            if obj is not None:
                return obj.validation_status == "quarantined"
        return False

    def get_field_provenance(self, record_id: str) -> dict[str, Any] | None:
        for model_cls in [models.DailyBarModel, models.WeeklyBarModel, models.MinuteBarModel, models.SecurityModel, models.ValuationMetricModel, models.FinancialIndicatorModel]:
            obj = self.session.execute(select(model_cls).where(model_cls.record_id == record_id)).scalar_one_or_none()
            if obj is not None:
                return obj.field_provenance
        return None
