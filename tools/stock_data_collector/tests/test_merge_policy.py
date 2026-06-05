from __future__ import annotations

from stock_data_ingestion.schemas.quality import MergeMethod, ValidationStatus
from stock_data_ingestion.validation.merge_policy import apply_canonical_merge_policy, mark_provider_specific_append

FIELDS = ["close", "volume", "amount"]


def test_canonical_validated_when_supplement_matches(bar_factory):
    canonical = bar_factory(provider="tushare", close=10.0)
    akshare = bar_factory(provider="akshare", close=10.0005)
    result = apply_canonical_merge_policy(canonical, [akshare], comparison_fields=FIELDS)
    merged = result.records[0]
    assert merged.provider == "tushare"
    assert merged.effective_provider == "tushare"
    assert merged.merge_method == MergeMethod.canonical_validated
    assert merged.validation_status == ValidationStatus.validated
    assert not result.conflicts


def test_supplements_never_override_tushare_and_mark_suspect(bar_factory):
    canonical = bar_factory(provider="tushare", close=10.0)
    akshare = bar_factory(provider="akshare", close=11.0)
    jq = bar_factory(provider="joinquant", close=11.0)
    result = apply_canonical_merge_policy(canonical, [akshare, jq], comparison_fields=FIELDS)
    merged = result.records[0]
    assert merged.provider == "tushare"
    assert merged.close == 10.0
    assert merged.canonical_value_suspect is True
    assert result.conflicts


def test_high_conflict_quarantines(bar_factory):
    canonical = bar_factory(provider="tushare", close=10.0)
    akshare = bar_factory(provider="akshare", close=12.0)
    result = apply_canonical_merge_policy(canonical, [akshare], comparison_fields=FIELDS, quarantine_on_critical_conflict=True)
    merged = result.records[0]
    assert merged.merge_method == MergeMethod.quarantined_due_to_conflict
    assert merged.validation_status == ValidationStatus.quarantined


def test_fallback_single_and_multi_source(bar_factory):
    akshare = bar_factory(provider="akshare", close=10.0)
    single = apply_canonical_merge_policy(None, [akshare], comparison_fields=FIELDS)
    assert single.records[0].merge_method == MergeMethod.fallback_single_source
    jq = bar_factory(provider="joinquant", close=10.0005)
    multi = apply_canonical_merge_policy(None, [akshare, jq], comparison_fields=FIELDS)
    assert multi.records[0].merge_method == MergeMethod.fallback_multi_source_agreed


def test_provider_specific_append_keeps_records_by_provider(bar_factory):
    industry = bar_factory(record_type="industry_membership", provider="akshare")
    result = mark_provider_specific_append([industry])
    assert result.records[0].merge_method == MergeMethod.provider_specific_append
