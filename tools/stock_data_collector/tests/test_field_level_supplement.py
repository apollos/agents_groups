from __future__ import annotations

from stock_data_ingestion.schemas.quality import MergeMethod
from stock_data_ingestion.validation.merge_policy import apply_canonical_merge_policy


def test_field_level_supplement_only_whitelisted_missing_fields(bar_factory):
    canonical = bar_factory(provider="tushare", turnover_rate_free_float=None)
    supplement = bar_factory(provider="akshare", turnover_rate_free_float=1.23)
    result = apply_canonical_merge_policy(
        canonical,
        [supplement],
        comparison_fields=["close", "volume", "amount"],
        supplement_field_whitelist=["turnover_rate_free_float"],
    )
    merged = result.records[0]
    assert merged.merge_method == MergeMethod.fill_missing_from_supplement
    assert merged.turnover_rate_free_float == 1.23
    assert merged.field_provenance["turnover_rate_free_float"]["source_role"] == "supplement"
