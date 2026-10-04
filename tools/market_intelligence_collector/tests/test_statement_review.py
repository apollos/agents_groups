"""Codex e2e review b62a210 (2026-10-04), F1 / F2 regression: Q1, Q2, Q3.

Fixture provenance: the three body passages are the exact model-input passages
of the EnergyTrend / BJX articles saved in the review run (``content-review.json``
``evidence_locator.excerpt``); the model phrasings below are the formal records
that leaked (``metric_f94f3891e3ca``, ``evt_fe2b0dc639d6``, ``evt_47da24c17a62``,
``fact_9ba8e14d920a``, brief ``why_it_matters``) plus paraphrases. Expected
results come from what the passages state, not from the model's own answer.
"""
from __future__ import annotations

import copy

from mic.schemas import Passage
from mic.validate import BundleValidator

P0 = "2026年9月15日，河北任丘智弘100MW/400MWh新型技术路线磷酸铁锂电池+钠电池独立储能试点项目储能系统设备采购中标结果公示。"
P1 = ("项目共两个标段，一标段为磷酸铁锂电池储能系统，30MW/120MWh大容量磷酸铁锂+60MW/240MWh长寿命磷酸铁锂，"
      "远景能源中标价为19461.6万元，合单价0.518元/Wh；")
P2 = "二标段为钠电池储能系统，10MW/40MWh钠离子储能系统，宁德时代中标价为4141.622万元，合单价1.035元/Wh。"
PASSAGES = [Passage(passage_id="p0", section="正文", text=P0),
            Passage(passage_id="p1", section="正文", text=P1),
            Passage(passage_id="p2", section="正文", text=P2)]


def validate(raw, passages=PASSAGES):
    return BundleValidator({"strict_evidence_review": True}).validate(raw, passages)


def metric(interpretation, channels=("revenue",), pid="p2", value=4141.622, unit="万元", name="宁德时代中标金额"):
    return {"metric_name": name, "metric_value": value, "unit": unit, "interpretation": interpretation,
            "impact_channels": list(channels), "evidence_locator": {"passage_id": pid}, "confidence": 0.8}


def event(summary, impact, pid="p1", subject="远景能源", event_type="major_order"):
    return {"event_type": event_type, "summary": summary, "entities": {"subject": subject, "product": "储能系统"},
            "metrics": {}, "impact": impact, "evidence_locator": {"passage_id": pid}, "confidence": 0.8}


def fact(statement, pid="p2", fact_type="price"):
    return {"fact_type": fact_type, "fact_statement": statement, "metrics": {"currency": "CNY", "unit": "元/Wh"},
            "evidence_locator": {"passage_id": pid}, "confidence": 0.8}


# --- Q1 materiality -----------------------------------------------------------

MATERIALITY_PARAPHRASES = [
    "宁德时代该储能订单金额，规模相对公司整体营收体量较小。",       # leaked in the review run
    "订单金额绝对规模较小，对宁德时代整体营收贡献有限。",           # was caught before
    "该订单占公司年度收入的比重很低。",
    "相对宁德时代的业绩体量，该金额影响不大。",
    "此单对公司利润影响显著。",
]


def test_q1_equivalent_materiality_claims_are_all_held_and_amount_is_preserved():
    for text in MATERIALITY_PARAPHRASES:
        raw = {"metrics": [metric(text)]}
        before = copy.deepcopy(raw)
        m = validate(raw).bundle.metrics[0]
        assert m.metric_value == 4141.622 and m.unit == "万元", text
        assert m.impact_channels == [], text
        assert "待核查" in m.interpretation and "营收" not in m.interpretation and "较小" not in m.interpretation, text
        held = m.scope["materiality_review"]
        assert held["status"] == "pending_review" and held["interpretation"] == text
        assert m.evidence_locator.excerpt == P2
        assert raw == before  # model output untouched


def test_q1_materiality_with_base_figure_in_passage_is_kept():
    passage = Passage(passage_id="p9", section="正文",
                      text="公司2025年营业收入3620亿元；本次中标金额4141.622万元，占营收比重约0.01%。")
    raw = {"metrics": [metric("本次中标金额占公司营收比重约0.01%，对收入影响很小。", pid="p9")]}
    m = validate(raw, [passage]).bundle.metrics[0]
    assert "materiality_review" not in m.scope
    assert m.interpretation.startswith("本次中标金额占公司营收比重约0.01%")
    assert m.impact_channels == ["revenue"]


def test_q1_literal_source_statement_is_kept():
    passage = Passage(passage_id="p9", section="正文", text="公司表示该订单对当期收入贡献有限。")
    raw = {"facts": [fact("公司表示该订单对当期收入贡献有限。", pid="p9", fact_type="order")]}
    f = validate(raw, [passage]).bundle.facts[0]
    assert f.fact_statement == "公司表示该订单对当期收入贡献有限。"
    assert "statement_review" not in f.metrics


def test_q1_materiality_in_fact_statement_and_event_summary_is_held_not_only_in_metrics():
    raw = {"facts": [fact("宁德时代中标金额4141.622万元，相对公司整体营收体量较小。", fact_type="order")],
           "events": [event("宁德时代中标4141.622万元钠电储能系统，对公司收入贡献微小。",
                            {"direction": "positive", "channels": ["revenue"]}, pid="p2", subject="宁德时代")]}
    b = validate(raw).bundle
    f, e = b.facts[0], b.events[0]
    assert f.fact_statement == "宁德时代中标金额4141.622万元；订单对公司收入的贡献尚未核实。"
    assert f.metrics["statement_review"]["held_clauses"][0]["kind"] == "materiality"
    assert "贡献微小" not in e.summary and "尚未核实" in e.summary
    assert e.metrics["statement_review"]["original"].endswith("对公司收入贡献微小。")
    # Observation + revenue channel of the target's own award is not a competition/economics claim.
    assert e.impact.direction == "positive" and e.impact.channels == ["revenue"]


# --- Q2 competition -------------------------------------------------------------

def test_q2_rival_award_in_other_lot_is_not_a_competition_impact():
    negative = event("远景能源中标一标段磷酸铁锂电池储能系统（360MWh），金额19461.6万元、单价0.518元/Wh。",
                     {"direction": "negative", "channels": ["competition"], "horizon": "quarter", "magnitude_guess": "low"})
    neutral = event("同项目一标段磷酸铁锂电池储能系统由远景能源中标，价19461.6万元、单价0.518元/Wh，构成宁德时代在储能招标中的同台竞争参照。",
                    {"direction": "neutral", "channels": ["competition"], "horizon": "1m", "magnitude_guess": "low"},
                    event_type="tender")
    b = validate({"events": [negative, neutral]}).bundle
    for e in b.events:
        # The award observation survives.
        assert "远景能源" in e.summary and "19461.6万元" in e.summary and "0.518元/Wh" in e.summary
        # The competition reading does not.
        assert e.impact.direction == "unclear" and e.impact.channels == [] and e.impact.horizon == "unclear"
        review = e.metrics["impact_review"]
        assert review["status"] == "pending_review" and review["reasons"] == ["competition"]
        assert review["impact"]["channels"] == ["competition"]
    assert b.events[0].metrics["impact_review"]["impact"]["direction"] == "negative"
    second = b.events[1]
    assert "同台竞争" not in second.summary
    assert second.metrics["statement_review"]["held_clauses"] == [
        {"clause": "构成宁德时代在储能招标中的同台竞争参照", "kind": "competition"}]


def test_q1_bare_size_judgements_from_batch_v3_are_held_but_stated_ratios_are_kept():
    """Batch v3 leaks: ``metric_65f97ce281ed`` / ``metric_a60797d50eda`` / brief why_it_matters."""
    raw = {"brief": {"one_sentence": "中标公示。", "what_happened": "两标段分别由远景能源与宁德时代中标。",
                     "why_it_matters": "宁德时代获得钠离子电池储能系统订单；体量虽小，但为钠电路线商业化验证提供价格与订单锚点。",
                     "uncertainty": "来源为媒体转载。"},
           "metrics": [metric("宁德时代钠电储能系统单笔中标金额，体量较小，具技术验证与订单锚点意义。"),
                       metric("10MW/40MWh钠离子储能系统，规模较小，具试点性质", channels=("demand",), value=40.0, unit="MWh", name="二标段钠电规模"),
                       metric("对应10MW/40MWh，占项目总规模400MWh的10%。", value=40.0, unit="MWh", name="钠离子储能系统规模"),
                       metric("同项目锂电标段中标额，规模远大于钠电标段。", pid="p1", value=19461.6, name="磷酸铁锂储能系统中标金额")]}
    b = validate(raw).bundle
    amount, scale, ratio, larger = b.metrics
    assert amount.scope["materiality_review"]["interpretation"].startswith("宁德时代钠电储能系统单笔中标金额，体量较小")
    assert "体量较小" not in amount.interpretation and amount.impact_channels == []
    assert "规模较小" not in scale.interpretation and "具试点性质" in scale.interpretation   # 试点项目 is in P0
    assert scale.interpretation.endswith("订单对公司收入的贡献尚未核实。")
    # A ratio with its base, and a comparison between two quoted amounts, are observations.
    assert ratio.interpretation == "对应10MW/40MWh，占项目总规模400MWh的10%。" and "materiality_review" not in ratio.scope
    assert larger.interpretation == "同项目锂电标段中标额，规模远大于钠电标段。" and "materiality_review" not in larger.scope
    assert "体量虽小" not in b.brief.why_it_matters and "价格与订单锚点" in b.brief.why_it_matters
    assert "；但" not in b.brief.why_it_matters   # contrast word of the dropped clause goes with it


def test_q2_impact_on_target_from_another_partys_award_is_held_when_target_is_known():
    """Batch v3 ``evt_cb752f86a31a``: 远景能源 lot 1, impact neutral/revenue — no channel to 宁德时代 in the passage."""
    rival = event("远景能源中标同项目一标段磷酸铁锂电池储能系统，中标价19461.6万元（0.518元/Wh）。",
                  {"direction": "neutral", "channels": ["revenue"], "horizon": "quarter", "magnitude_guess": "low"})
    own = event("宁德时代中标二标段10MW/40MWh钠离子储能系统，中标价4141.622万元（1.035元/Wh）。",
                {"direction": "positive", "channels": ["revenue"], "horizon": "quarter", "magnitude_guess": "low"},
                pid="p2", subject="宁德时代")
    validator = BundleValidator({"strict_evidence_review": True}, target_names=["宁德时代", "CATL"])
    b = validator.validate({"events": [rival, own]}, PASSAGES).bundle
    held, kept = b.events
    assert held.impact.channels == [] and held.impact.direction == "unclear"
    assert held.metrics["impact_review"]["impact"] == {"direction": "neutral", "channels": ["revenue"],
                                                        "horizon": "quarter", "magnitude_guess": "low"}
    assert kept.impact.direction == "positive" and kept.impact.channels == ["revenue"] and "impact_review" not in kept.metrics
    # Without a known target the rule cannot apply and nothing is invented.
    b2 = validate({"events": [copy.deepcopy(rival)]}).bundle
    assert b2.events[0].impact.channels == ["revenue"]


def test_q2_competition_with_passage_evidence_is_kept():
    passage = Passage(passage_id="p9", section="正文",
                      text="该标段宁德时代与远景能源同台竞标，最终远景能源中标，宁德时代落标。")
    raw = {"events": [event("宁德时代在该标段落标，远景能源中标，构成直接竞争失利。",
                            {"direction": "negative", "channels": ["competition"], "horizon": "quarter"}, pid="p9")]}
    e = validate(raw, [passage]).bundle.events[0]
    assert e.impact.direction == "negative" and e.impact.channels == ["competition"]
    assert "impact_review" not in e.metrics and "statement_review" not in e.metrics


# --- Q3 price basis ------------------------------------------------------------

def _price_bundle():
    return {
        "brief": {"one_sentence": "中标公示。", "what_happened": "两标段分别由远景能源与宁德时代中标。",
                  "why_it_matters": ("宁德时代在钠离子电池储能这一新路线上拿到公开招标订单，是钠电产品工程化落地的可核验价格与规模证据；"
                                     "同时同业远景能源在磷酸铁锂标段中标，提供同项目磷酸铁锂与钠电单位价格的可比参照。"),
                  "uncertainty": "来源为行业媒体转载。"},
        "facts": [fact("宁德时代钠离子储能系统中标单价1.035元/Wh，明显高于远景能源磷酸铁锂储能系统的0.518元/Wh。")],
        "metrics": [metric("同项目磷酸铁锂储能系统中标单价，可作为钠电价格的对照基准。", channels=("cost", "margin"),
                           pid="p1", value=0.518, unit="元/Wh", name="磷酸铁锂储能系统中标单价"),
                    metric("钠电储能系统中标单价。", channels=(), pid="p2", value=1.035, unit="元/Wh", name="钠离子储能系统中标单价")],
    }


def test_q3_price_basis_limitation_reaches_fact_metric_and_brief_consistently():
    b = validate(_price_bundle()).bundle
    # Metric: existing behaviour (basis mismatch 19461.6万元/360MWh = 0.5406 ≠ 0.518), source quote preserved.
    lfp = b.metrics[0]
    assert lfp.metric_value == 0.518
    assert lfp.scope["source_price_basis_status"] == "pending_review"
    assert lfp.scope["usable_as_price_benchmark"] is False
    assert lfp.scope["source_price_basis_review"]["computed_price_yuan_per_Wh"].startswith("0.5406")
    assert "基准" not in lfp.interpretation and lfp.impact_channels == []
    # Fact: cross-passage comparison keeps both quotations with both-sided evidence and the limitation.
    f = b.facts[0]
    assert "1.035元/Wh" in f.fact_statement and "0.518元/Wh" in f.fact_statement
    assert f.metrics["comparison_evidence"] == [{"value": "0.518", "passage_id": "p1"}]
    assert f.metrics["source_price_basis_status"] == "pending_review"
    assert f.metrics["usable_as_price_benchmark"] is False
    assert f.metrics["price_basis_passages"] == ["p1"]
    # Brief: no formal "comparable benchmark" conclusion; the award observation stays.
    why = b.brief.why_it_matters
    assert "可比参照" not in why
    assert "远景能源在磷酸铁锂标段中标" in why
    assert "供货范围与口径尚未核实" in why
    # Ledger entry carries the original wording.
    paths = {(q["path"], q["action"]) for q in validate(_price_bundle()).quality_reviews}
    assert ("brief.why_it_matters", "hold_price_basis") in paths


def test_q3_comparison_against_a_price_not_in_any_passage_is_held():
    raw = {"facts": [fact("宁德时代钠离子储能系统中标单价1.035元/Wh，高于行业均价0.6元/Wh。")]}
    f = validate(raw).bundle.facts[0]
    assert f.metrics["comparison_review"]["unsupported_prices"] == ["0.6"]
    assert "0.6元/Wh" not in f.fact_statement and "待核查" in f.fact_statement


def test_q3_without_basis_mismatch_no_limitation_is_invented():
    # Consistent amount / capacity / quote: 36000万元 / 360MWh = 1.0元/Wh.
    p1 = Passage(passage_id="p1", section="正文", text="一标段360MWh，远景能源中标价为36000万元，合单价1元/Wh；")
    p2 = Passage(passage_id="p2", section="正文", text="二标段40MWh，宁德时代中标价为4140万元，合单价1.035元/Wh。")
    raw = {"facts": [fact("宁德时代中标单价1.035元/Wh，高于远景能源的1元/Wh。")],
           "metrics": [metric("磷酸铁锂标段中标单价。", channels=(), pid="p1", value=1, unit="元/Wh", name="单价")]}
    b = validate(raw, [p1, p2]).bundle
    assert b.facts[0].metrics["comparison_evidence"] == [{"value": "1", "passage_id": "p1"}]
    assert "source_price_basis_status" not in b.facts[0].metrics
    assert "source_price_basis_status" not in b.metrics[0].scope


def test_holding_texts_are_not_re_reviewed_idempotently():
    raw = _price_bundle()
    first = validate(raw).bundle
    second = validate(first.model_dump(mode="json")).bundle
    assert second.metrics[0].interpretation == first.metrics[0].interpretation
    assert second.brief.why_it_matters == first.brief.why_it_matters
    assert second.facts[0].fact_statement == first.facts[0].fact_statement
