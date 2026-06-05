from __future__ import annotations

import pytest
from pydantic import ValidationError

from stock_data_ingestion.schemas.records import BarRecord


def test_populated_business_field_without_provenance_is_rejected(bar_factory):
    data = bar_factory().model_dump(mode="python")
    data["field_provenance"].pop("open")
    with pytest.raises(ValidationError):
        BarRecord(**data)
