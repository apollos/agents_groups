"""V0.8 research-loop tests: event tracking_variables and cache-reuse event details."""

from __future__ import annotations

from mic.modeling.prompts import SCHEMA_HINT, build_bundle_messages
from mic.profile import TargetProfile
from mic.schemas import BundleExtraction, EventCard, TrackingVariableEvidence
from mic.store.database import get_database
from mic.store.repository import Repository


def _profile() -> TargetProfile:
    return TargetProfile.from_config(
        {
            "target_id": "company_002371",
            "type": "company",
            "canonical_name": "北方华创",
            "tracking_variables": ["orders", "gross_margin", "inventory"],
            "theme_ids": ["industry_export_manufacturing"],
        }
    )


def test_mic_schema_accepts_event_tracking_variables():
    event = EventCard.model_validate(
        {
            "event_type": "major_order",
            "summary": "签订重大订单",
            "confidence": 0.9,
            "tracking_variables": [
                {"variable": "orders", "direction": "positive", "strength": 0.8, "confidence": 0.9, "reasoning": "中标公告"}
            ],
        }
    )
    assert event.tracking_variables[0].variable == "orders"
    assert isinstance(event.tracking_variables[0], TrackingVariableEvidence)
    # Absent field defaults to an empty list, so older model outputs still validate.
    legacy = EventCard.model_validate({"event_type": "major_order", "summary": "旧格式"})
    assert legacy.tracking_variables == []


def test_profile_carries_tracking_variables_and_theme_ids():
    profile = _profile()
    assert profile.tracking_variables == ["orders", "gross_margin", "inventory"]
    assert profile.theme_ids == ["industry_export_manufacturing"]


def test_prompt_includes_target_tracking_variables():
    messages = build_bundle_messages(_profile(), {"source_link_id": "l1"}, [], {})
    user_payload = messages[1]["content"]
    assert '"tracking_variables"' in user_payload
    assert "orders" in user_payload
    # System prompt instructs the model to only pick from the declared list.
    assert "tracking_variables" in messages[0]["content"]
    hint_events = SCHEMA_HINT["events"][0]
    assert "tracking_variables" in hint_events


def test_clone_latest_analysis_returns_cloned_event_details(config):
    repo = Repository(get_database(config.database_url))
    bundle = BundleExtraction(
        source_link_id="link_a",
        decision="save_structured",
        events=[
            EventCard(event_type="major_order", event_date="2026-07-03", summary="中标特高压项目", confidence=0.9)
        ],
    )
    repo.save_merged_analysis("company_002371", "link_a", bundle, {})
    cloned = repo.clone_latest_analysis("link_a", "link_b", "company_002371")
    assert cloned["events"] == 1
    details = cloned["cloned_events"]
    assert len(details) == 1
    assert details[0]["summary"] == "中标特高压项目"
    assert details[0]["event_type"] == "major_order"
    assert str(details[0]["event_date"]).startswith("2026-07-03")
    assert details[0]["source_link_id"] == "link_b"
    assert details[0]["confidence"] == 0.9
