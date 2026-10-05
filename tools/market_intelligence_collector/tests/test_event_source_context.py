"""Replay the two saved article layouts that dropped p0 behind a title."""
import json
from pathlib import Path

import pytest

from mic.event_resolution import build_context, finalize, fingerprint
from mic.schemas import BundleExtraction


FIXTURE = json.loads((Path(__file__).parent / "fixtures/event_context_run_88f8aa20b573.json").read_text())


@pytest.mark.parametrize("sample", FIXTURE["samples"], ids=lambda sample: sample["source_link_id"])
def test_title_does_not_displace_original_article_intro(sample):
    bundle = BundleExtraction(events=[{"evidence_locator": sample["evidence_locator"]}])
    finalize(bundle, build_context(None, []), sample["passages"],
             run_id=FIXTURE["search_run_id"], link_id=sample["source_link_id"])
    event = bundle.events[0].model_dump(mode="json")
    # Both live inputs put the title first and cite p2. p0 carries the project
    # name/date and must survive alongside the cited lot and its neighbour.
    assert [p["passage_id"] for p in event["source_context"]][:4] == ["title", "p0", "p1", "p2"]
    assert event["source_context"] == sample["passages"]
    assert event["event_resolution"]["event_fingerprint"] == fingerprint(
        {**event, "source_link_id": sample["source_link_id"]})
