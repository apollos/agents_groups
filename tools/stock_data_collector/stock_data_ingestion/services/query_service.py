from __future__ import annotations

from datetime import date
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from stock_data_ingestion.normalization.datetime_utils import normalize_trade_date
from stock_data_ingestion.normalization.ticker import normalize_ticker
from stock_data_ingestion.storage import models


class QueryService:
    def __init__(self, session: Session) -> None:
        self.session = session

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

    def get_bars(self, ticker: str, start_date: str | date, end_date: str | date, frequency: str = "1d", adjust: str = "none") -> pd.DataFrame:
        model_cls = models.BAR_MODEL_BY_FREQUENCY.get(frequency)
        if model_cls is None:
            raise ValueError(f"INVALID_REQUEST: unsupported frequency {frequency}")
        normalized = normalize_ticker(ticker)
        start = normalize_trade_date(start_date)
        end = normalize_trade_date(end_date)
        stmt = (
            select(model_cls)
            .where(
                model_cls.normalized_ticker == normalized,
                model_cls.trade_date >= start,
                model_cls.trade_date <= end,
                model_cls.frequency == frequency,
                model_cls.adjust == adjust,
            )
            .order_by(model_cls.trade_date, model_cls.timestamp)
        )
        return self._rows_to_df(self.session.execute(stmt).scalars().all())

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
