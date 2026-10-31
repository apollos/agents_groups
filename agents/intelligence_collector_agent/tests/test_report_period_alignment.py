"""Stock report periods stay clues until MIC publishes the same period.

Supplier amounts may be compared, but they must not leave this module.
"""
import json
from pathlib import Path

from agent_trade_intel.db import SQLiteStore, dumps_json
from agent_trade_intel.reader import IntelligenceReader
from agent_trade_intel.report_period_alignment import align_report_periods, period_end


def _statement(**overrides):
    row = {
        "record_id": "rec-q3",
        "statement_type": "income_statement",
        "report_period": "20260930",
        "announcement_date": "2026-10-29",
        "effective_provider": "tushare",
        "operating_revenue": 3288616730.51,
        "parent_net_profit": 37697317.32,
        "operating_cash_flow": None,
    }
    row.update(overrides)
    return row


def _observations():
    return [
        {"metric_id": "m-h1", "period": "2026年半年度", "source_link_id": "link-h1"},
        {"metric_id": "m-fy", "period": "2025年度", "source_link_id": "link-fy"},
        {"metric_id": "m-prior", "period": "上年同期（2024年度）", "source_link_id": "link-fy"},
    ]


def test_chinese_and_compact_periods_share_an_end_date():
    assert period_end("20260930").isoformat() == "2026-09-30"
    assert period_end("2026年半年度").isoformat() == "2026-06-30"
    assert period_end("2026年前三季度").isoformat() == "2026-09-30"
    assert period_end("上年同期（2024年度）").isoformat() == "2024-12-31"
    assert period_end("上年同期") is None


def test_later_stock_period_becomes_a_gap_without_supplier_amounts():
    result = align_report_periods(
        [
            _statement(),
            _statement(record_id="rec-q3-cash", statement_type="cash_flow",
                       operating_revenue=None, parent_net_profit=None,
                       operating_cash_flow=-3309197894.09, effective_provider="akshare"),
            _statement(record_id="rec-h1", report_period="20260630", announcement_date="2026-08-10"),
        ],
        _observations(),
        demand_id="demand-1",
        directory_unresolved=True,
    )
    assert result["status"] == "later_period_unread"
    assert result["stock_report_period"] == "20260930"
    assert result["mic_latest_period"] == "2026年半年度"
    assert result["announcement_dates"] == ["2026-10-29"]
    assert result["providers"] == ["akshare", "tushare"]
    assert result["core_fields_consistent"] is True
    gap = result["gap"]
    assert gap["gap_type"] == "later_report_period_unread"
    assert "20260930" in gap["description"]
    assert "2026-10-29" in gap["description"]
    assert "2026年半年度" in gap["description"]
    assert "目录" in gap["description"]
    encoded = json.dumps(result, ensure_ascii=False)
    assert "3288616730.51" not in encoded
    assert "37697317.32" not in encoded
    assert "-3309197894.09" not in encoded
    assert "operating_revenue" not in encoded


def test_same_period_end_does_not_open_a_gap():
    result = align_report_periods(
        [_statement(report_period="20260630")],
        [{"metric_id": "m-h1", "period": "2026年半年度", "source_link_id": "link-h1"}],
    )
    assert result["status"] == "no_later_stock_period"
    assert result["gap"] is None


def test_conflicting_supplier_values_stay_out_of_the_gap():
    result = align_report_periods(
        [
            _statement(effective_provider="tushare", operating_revenue=3288616730.51),
            _statement(record_id="rec-other", effective_provider="akshare", operating_revenue=1111111111.11),
        ],
        _observations(),
    )
    assert result["core_fields_consistent"] is False
    encoded = json.dumps(result, ensure_ascii=False)
    assert "3288616730.51" not in encoded
    assert "1111111111.11" not in encoded


def test_missing_period_list_is_not_treated_as_an_empty_publication():
    result = align_report_periods([_statement()], None)
    assert result["status"] == "mic_periods_unavailable"
    assert result["gap"] is None


def test_collection_status_reports_the_alignment(tmp_path: Path):
    store = SQLiteStore(tmp_path / "data.db")
    store.init_schema()
    bus = SQLiteStore(tmp_path / "bus.db")
    bus.init_schema()
    stock_result = {
        "returncode": 0,
        "stdout": {"data": {"financial_statements": [_statement()]}},
    }
    mic_result = {
        "published_metric_periods": _observations(),
        "collection_diagnostics": {"read_failures": {"index_scope_unresolved": 2}},
    }
    with store.session() as con:
        con.execute(
            "INSERT INTO collection_runs(run_id, demand_id, tool_name, operation, status, result_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("run-stock", "demand-1", "stock_data_collector", "fetch_financial_statement",
             "success", dumps_json(stock_result)),
        )
        con.execute(
            "INSERT INTO collection_runs(run_id, demand_id, tool_name, operation, status, result_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("run-mic", "demand-1", "market_intelligence_collector", "collect_intelligence",
             "success", dumps_json(mic_result)),
        )
    delivered = IntelligenceReader(store, bus).read_collection_status(demand_id="demand-1")
    assert delivered["report_period_alignment"][0]["status"] == "later_period_unread"
    assert delivered["report_period_alignment"][0]["directory_unresolved"] is True
    assert "3288616730.51" not in json.dumps(delivered["report_period_alignment"], ensure_ascii=False)
