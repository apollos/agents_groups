from __future__ import annotations

import pytest
from pydantic import ValidationError

from stock_data_ingestion.normalization.units import normalize_amount, normalize_volume
from stock_data_ingestion.schemas.records import BarRecord


def test_bar_record_requires_key_fields_and_adjust(bar_factory):
    bar = bar_factory()
    assert bar.adjust == "qfq"
    assert bar.close == 10.0
    data = bar.model_dump(mode="python")
    data.pop("close")
    with pytest.raises(ValidationError):
        BarRecord(**data)
    data = bar.model_dump(mode="python")
    data["adjust"] = "unknown"
    with pytest.raises(ValidationError):
        BarRecord(**data)


def test_bar_record_requires_field_provenance(bar_factory):
    data = bar_factory().model_dump(mode="python")
    data["field_provenance"] = {}
    with pytest.raises(ValidationError):
        BarRecord(**data)


def test_volume_and_amount_units():
    assert normalize_volume(12.5, unit="hand") == (1250.0, "share")
    assert normalize_amount(2.5, unit="thousand_cny") == (2500.0, "CNY")
