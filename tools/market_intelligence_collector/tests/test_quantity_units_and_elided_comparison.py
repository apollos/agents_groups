"""Codex e2e review 693b3df (2026-10-05), R2 / R3 — T3 / T4.

Fixture provenance: the three body passages are the model-input passages of the
BJX / EnergyTrend copies of the 2026-09-15 award notice; the model outputs below
are the formal records of run ``run_3549da9f8002`` that leaked
(``metric_d40a8a1b598e``, ``metric_8faafc286d45``, ``evt_7eef7d8e1895``,
``evt_0648c820d9e5``, ``fact_d44835f67705``, ``fact_cbca8775d7fd``,
``fact_b24346cb8be2``) plus the review's variants.
"""
from __future__ import annotations

import copy

from mic.quantity_units import normalize_metrics_block, passage_quantities
from mic.schemas import Passage
from mic.validate import BundleValidator

P0 = "2026年9月15日，河北任丘智弘100MW/400MWh新型技术路线磷酸铁锂电池+钠电池独立储能试点项目储能系统设备采购中标结果公示。"
P1 = ("项目共两个标段，一标段为磷酸铁锂电池储能系统，30MW/120MWh大容量磷酸铁锂+60MW/240MWh长寿命磷酸铁锂，"
      "远景能源中标价为19461.6万元，合单价0.518元/Wh；")
P2 = "二标段为钠电池储能系统，10MW/40MWh钠离子储能系统，宁德时代中标价为4141.622万元，合单价1.035元/Wh。"
PASSAGES = [Passage(passage_id="p0", section="正文", text=P0),
            Passage(passage_id="p1", section="正文", text=P1),
            Passage(passage_id="p2", section="正文", text=P2)]


def validate(raw, passages=PASSAGES, strict=True):
    return BundleValidator({"strict_evidence_review": strict}).validate(raw, passages)


def metric(name, value, unit, pid, interpretation="来源数值。"):
    return {"metric_name": name, "metric_value": value, "unit": unit, "interpretation": interpretation,
            "impact_channels": [], "evidence_locator": {"passage_id": pid}, "confidence": 0.8}


def event(summary, metrics, pid, subject, event_type="tender"):
    return {"event_type": event_type, "event_date": "2026-09-15", "summary": summary,
            "entities": {"subject": subject, "product": "储能系统"}, "metrics": metrics,
            "impact": {"direction": "neutral", "channels": ["demand"]},
            "evidence_locator": {"passage_id": pid}, "confidence": 0.8}


def fact(statement, metrics, pid, fact_type="order"):
    return {"fact_type": fact_type, "fact_statement": statement, "metrics": metrics,
            "evidence_locator": {"passage_id": pid}, "confidence": 0.8}


# --- T3: power vs energy ----------------------------------------------------------

def test_passage_quantities_separate_power_and_energy():
    got = [(q["value"], q["unit"], q["kind"], q["canonical"]) for q in passage_quantities(P0)]
    assert got == [(100.0, "MW", "power", 100.0), (400.0, "MWh", "energy", 400.0)]
    assert [(q["value"], q["kind"]) for q in passage_quantities(P1)] == [(30.0, "power"), (120.0, "energy"), (60.0, "power"), (240.0, "energy")]
    assert [(q["canonical"], q["kind"]) for q in passage_quantities("0.4GWh / 400 kW / 2GW")] == [(400.0, "energy"), (0.4, "power"), (2000.0, "power")]


def test_mixed_metric_unit_is_split_into_power_value_and_evidenced_energy():
    raw = {"metrics": [metric("项目储能系统总规模", 100, "MW/400MWh", "p0"),      # metric_d40a8a1b598e
                       metric("二标段钠离子储能系统规模", 10, "MW/40MWh", "p2")]}  # metric_8faafc286d45
    before = copy.deepcopy(raw)
    b = validate(raw).bundle
    total, lot2 = b.metrics
    assert (total.metric_value, total.unit) == (100.0, "MW") and total.scope["unit_raw"] == "MW/400MWh"
    assert total.scope["energy_mwh"] == {"value": 400.0, "unit": "MWh", "kind": "energy", "canonical": 400.0,
                                         "evidence": {"passage_id": "p0", "quote": "400MWh"}}
    assert total.scope["power_mw"]["canonical"] == 100.0 and total.scope["power_mw"]["evidence"]["quote"] == "100MW"
    assert (lot2.metric_value, lot2.unit) == (10.0, "MW") and lot2.scope["energy_mwh"]["canonical"] == 40.0
    assert raw == before   # the model response is not mutated


def test_metric_unit_contradicted_by_passage_is_corrected_with_evidence_or_marked_unverified():
    raw = {"metrics": [metric("项目规模", 100, "MWh", "p0"),      # passage says 100MW
                       metric("合计能量", 360, "MWh", "p1"),      # explicit "120MWh+240MWh" sum (existing gate rule)
                       metric("规模", 500, "MWh", "p0"),          # never stated → strict gate quarantines it
                       metric("二标段能量", 40, "MWh", "p2")]}    # correct
    report = validate(raw)
    wrong, summed, right = report.bundle.metrics
    assert wrong.unit == "MW" and wrong.scope["unit_review"] == {"status": "corrected_from_passage", "model_unit": "MWh",
                                                                  "passage_quote": "100MW"}
    assert summed.scope["energy_mwh"]["canonical"] == 360.0 and summed.scope["energy_mwh"]["operands"] == [120, 240]
    quarantined = [q for q in report.quality_reviews if q["path"] == "metrics[2]"]
    assert quarantined and quarantined[0]["action"] == "quarantine_metric"
    assert quarantined[0]["original"]["scope"]["unit_review"]["status"] == "unit_unverified"
    assert right.scope["energy_mwh"]["canonical"] == 40.0 and "unit_review" not in right.scope


def test_event_capacity_and_volume_resolve_to_canonical_energy_from_the_passage():
    bjx = event("河北任丘智弘100MW/400MWh新型技术路线储能试点项目储能系统采购中标结果公示，两个标段分别由远景能源与宁德时代中标。",
                {"amount": None, "currency": None, "capacity": 100, "volume": 400}, "p0", "河北任丘智弘独立储能试点项目")   # evt_7eef7d8e1895
    et = event("河北任丘智弘100MW/400MWh…中标结果公示，两标段分别由远景能源与宁德时代中标。",
               {"amount": None, "currency": "CNY", "capacity": 400, "volume": None}, "p0", "河北任丘智弘储能试点项目")     # evt_0648c820d9e5
    a, b = validate({"events": [bjx, et]}).bundle.events
    assert a.metrics["power_mw"] == 100.0 and a.metrics["capacity_unit"] == "MW"
    assert a.metrics["energy_mwh"] == 400.0 and a.metrics["volume_unit"] == "MWh"
    assert a.metrics["energy_evidence"] == {"passage_id": "p0", "quote": "400MWh", "field": "volume"}
    assert b.metrics["energy_mwh"] == 400.0 and b.metrics["capacity_unit"] == "MWh"
    assert a.metrics["capacity"] == 100 and b.metrics["capacity"] == 400     # originals kept as stated
    # Both copies of the same notice now carry the same energy for identity purposes.
    assert a.metrics["energy_mwh"] == b.metrics["energy_mwh"]


def test_fact_mixed_currency_energy_unit_is_split_and_unverified_numbers_stay_unknown():
    catl = fact("宁德时代中标河北任丘智弘100MW/400MWh独立储能试点项目二标段钠离子储能系统，中标价4141.622万元。",
                {"amount": 4141.622, "currency": "CNY", "amount_unit": "万元", "volume": 40, "unit": "万元/MWh"}, "p2")   # fact_d44835f67705
    envision = fact("远景能源中标一标段磷酸铁锂电池储能系统（30MW/120MWh大容量+60MW/240MWh长寿命），中标价19461.6万元。",
                    {"amount": 19461.6, "currency": "CNY", "amount_unit": "万元", "volume": 360, "unit": "万元/MWh"}, "p1")   # fact_cbca8775d7fd
    unitless = fact("项目总规模。", {"volume": 400}, "p2")                                                   # 400 is not in p2
    a, b, c = validate({"facts": [catl, envision, unitless]}).bundle.facts
    assert a.metrics["amount"] == 41416220 and a.metrics["amount_unit"] == "元"                              # money path unchanged
    assert a.metrics["unit"] == "MWh" and a.metrics["unit_raw"] == "万元/MWh"
    assert a.metrics["energy_mwh"] == 40.0 and a.metrics["energy_evidence"]["quote"] == "40MWh"
    assert b.metrics["unit"] == "MWh" and b.metrics["volume"] == 360
    assert b.metrics["energy_mwh"] == 360.0 and b.metrics["energy_evidence"]["quote"].startswith("30MW/120MWh")   # explicit sum
    assert "energy_mwh" not in c.metrics and c.metrics["volume_unit_status"] == "unit_unverified"


def test_metric_value_given_as_power_energy_token_is_split_instead_of_failing_schema():
    """Batch v5 BJX output: ``metric_value="10MW/40MWh"`` (string) made the whole bundle schema-invalid."""
    raw = {"metrics": [metric("二标段规模", "10MW/40MWh", "", "p2"), metric("项目规模", "100MW/400MWh", None, "p0"),
                       metric("中标金额", "4141.622万元", "万元", "p2")]}      # not a power/energy token → still an error
    report = validate(raw)
    assert not report.schema_valid and report.errors == ["schema: Input should be a valid number, unable to parse string as a number"]
    raw["metrics"].pop()
    report = validate(raw)
    assert report.schema_valid
    lot2, total = report.bundle.metrics
    assert (lot2.metric_value, lot2.unit) == (10.0, "MW") and lot2.scope["unit_raw"] == "MW/40MWh"
    assert lot2.scope["energy_mwh"]["canonical"] == 40.0 and lot2.scope["energy_mwh"]["evidence"]["quote"] == "40MWh"
    assert (total.metric_value, total.unit) == (100.0, "MW") and total.scope["energy_mwh"]["canonical"] == 400.0
    assert any("quantity string '10MW/40MWh' split" in w for w in report.warnings)
    # Event / fact fields with the token: both quantities, each verified.
    e = validate({"events": [event("公示。", {"capacity": "100MW/400MWh"}, "p0", "河北任丘智弘储能试点项目")]}).bundle.events[0]
    assert e.metrics["power_mw"] == 100.0 and e.metrics["energy_mwh"] == 400.0 and e.metrics["capacity_unit"] == "mixed"
    assert e.metrics["capacity"] == "100MW/400MWh"


def test_normalize_block_handles_gwh_and_explicit_units():
    block, notes = normalize_metrics_block({"capacity": 0.4, "capacity_unit": "GWh"}, "规模0.4GWh。", "p9")
    assert block["energy_mwh"] == 400.0 and not notes
    block, notes = normalize_metrics_block({"capacity": 400, "capacity_unit": "MWh"}, "规模0.4GWh。", "p9")
    assert "energy_mwh" not in block and block["capacity_unit_status"] == "unit_unverified"   # 400 MWh never stated


# --- T4: elided price comparison -------------------------------------------------

ELIDED = [
    "同项目磷酸铁锂电池储能系统中标单价为0.518元/Wh，显著低于钠电二标段。",   # fact_b24346cb8be2
    "磷酸铁锂储能系统中标单价0.518元/Wh，与另一标段相比更低。",
    "一标段磷酸铁锂中标单价0.518元/Wh，约为钠电标段的一半。",
]
EXPLICIT = "磷酸铁锂储能系统中标单价0.518元/Wh，低于钠电二标段的1.035元/Wh。"


def price_fact(statement):
    return fact(statement, {"amount": 0.518, "currency": "CNY", "unit": "元/Wh"}, "p1", fact_type="price")


def test_t4_elided_comparison_resolves_the_other_quotation_with_the_same_evidence_as_explicit():
    explicit = validate({"facts": [price_fact(EXPLICIT)]}).bundle.facts[0]
    for text in ELIDED:
        f = validate({"facts": [price_fact(text)]}).bundle.facts[0]
        assert f.fact_statement == text                                   # wording stands: both sides evidenced
        assert f.metrics["comparison_evidence"] == [{"value": "1.035", "passage_id": "p2", "resolved_from": "elided_reference"}]
        assert f.metrics["source_price_basis_status"] == "pending_review"
        assert f.metrics["usable_as_price_benchmark"] is False
        assert f.metrics["price_basis_passages"] == ["p1"]
        assert f.metrics["unit_price"] == 0.518                           # the quotation itself is untouched
    assert explicit.metrics["comparison_evidence"] == [{"value": "1.035", "passage_id": "p2"}]
    assert explicit.metrics["usable_as_price_benchmark"] is False


def test_t4_elided_comparison_without_a_resolvable_other_side_is_held_and_the_quote_kept():
    only_lot1 = [Passage(passage_id="p1", section="正文", text=P1)]
    f = validate({"facts": [price_fact(ELIDED[0])]}, only_lot1).bundle.facts[0]
    assert f.fact_statement == "同项目磷酸铁锂电池储能系统中标单价为0.518元/Wh；与其他报价的比较缺少另一侧的数值依据，待核查。"
    assert f.metrics["comparison_review"]["held_clauses"] == ["显著低于钠电二标段"]
    assert f.metrics["comparison_review"]["candidate_prices"] == []
    assert "comparison_evidence" not in f.metrics and f.metrics["unit_price"] == 0.518
    # Ambiguous: two other quotations in the document → held as well, candidates listed.
    many = [*PASSAGES, Passage(passage_id="p3", section="正文", text="另一项目钠电报价0.95元/Wh。")]
    g = validate({"facts": [price_fact(ELIDED[1])]}, many).bundle.facts[0]
    assert g.metrics["comparison_review"]["candidate_prices"] == ["0.95", "1.035"]
    assert g.fact_statement.startswith("磷酸铁锂储能系统中标单价0.518元/Wh；")


def test_t4_single_quotation_without_ordering_words_is_left_alone():
    f = validate({"facts": [price_fact("一标段磷酸铁锂电池储能系统中标单价为0.518元/Wh。")]}).bundle.facts[0]
    assert "comparison_evidence" not in f.metrics and "comparison_review" not in f.metrics
    assert f.fact_statement == "一标段磷酸铁锂电池储能系统中标单价为0.518元/Wh。"
