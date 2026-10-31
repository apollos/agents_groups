"""Coverage contract enforcement; these model doubles do not certify model semantics."""
from copy import deepcopy

import pytest

from mic.content_review import finalize_task_coverage
from mic.content_review_policy import PROTOCOL, RULE_TEXT, TASK_COVERAGE_PROTOCOL
from mic.modeling.prompts import SYSTEM_PROMPT
from mic.schemas import BundleExtraction, CoverageGap, Passage
from mic.task_questions import parse_task_questions, task_context
from mic.validate import BundleValidator


QUESTION = "核查交付、经营和融资进展，并提供原文依据"
QUESTIONS = task_context(parse_task_questions({"questions": [QUESTION]}))["questions"]
TEXT = "本季度已经交付设备。"


def assessment(status="partial", answers=None, aspects=None):
    return {"question_id": "q1", "status": status,
            "answer_claim_ids": (["answer"] if status != "unanswered" else []) if answers is None else answers,
            "uncovered_aspects": ([] if status == "covered" else ["经营", "融资进展"]) if aspects is None else aspects,
            "reason": "模型自由理由只能留在审计，不能当作发布事实。"}


def raw_bundle(rows=None, *, text=TEXT, claim_status="source_supported"):
    review = {"protocol": PROTOCOL,
              "claims": [{"id": "answer", "kind": "observation", "status": claim_status,
                          "statement": text, "reason": "原文描述交付进度。",
                          "evidence": [{"passage_id": "p1", "quote": text}], "depends_on": []}],
              "bindings": {"/facts/0": ["answer"]}}
    if rows is not None:
        review["task_coverage"] = rows
    return {"schema_version": "bundle_extraction_v0.3", "confidence": 0.0, "decision": "save_structured", "overall_score": 80,
            "facts": [{"fact_statement": text, "evidence_locator": {"passage_id": "p1"}}],
            "content_review": review}


def validated(raw, *, text=TEXT):
    report = BundleValidator({}).validate(raw, [Passage(passage_id="p1", section="正文", text=text)])
    assert report.schema_valid, report.errors
    report.bundle.source_link_id = "source_current"
    return report.bundle


def test_task_context_ids_and_canonical_prompt_contract():
    context = task_context(parse_task_questions({"questions": [QUESTION, "另一个问题"]}))
    assert [q["question_id"] for q in context["questions"]] == ["q1", "q2"]
    assert context["questions"][0]["question"] == QUESTION
    assert "content_review.task_coverage" in RULE_TEXT
    assert RULE_TEXT in SYSTEM_PROMPT


def test_partial_answer_publishes_question_scoped_gap_and_keeps_facts():
    bundle = validated(raw_bundle([assessment()]))
    before = bundle.facts[0].model_dump(mode="json")
    result = finalize_task_coverage(bundle, QUESTIONS)
    assert result[0]["status"] == "partial"
    assert result[0]["protocol"] == TASK_COVERAGE_PROTOCOL
    assert result[0]["question"] == QUESTION
    assert result[0]["source_link_id"] == "source_current"
    assert result[0]["answer_claim_ids"] == ["answer"]
    assert result[0]["answer_claim_refs"] == [{"contribution_index": 0, "claim_id": "answer"}]
    assert result[0]["uncovered_aspects"] == ["经营", "融资进展"]
    assert "未覆盖的需求原文：经营；融资进展" in bundle.coverage_gaps[0].description
    assert "模型自由理由" not in bundle.coverage_gaps[0].description
    assert bundle.facts[0].model_dump(mode="json") == before
    assert bundle.content_review["model_review"]["task_coverage"][0]["reason"].startswith("模型自由理由")


def test_covered_requires_explicit_assessment_not_fact_count():
    explicit = validated(raw_bundle([assessment("covered")]))
    assert finalize_task_coverage(explicit, QUESTIONS)[0]["status"] == "covered"
    assert explicit.coverage_gaps == []
    missing = validated(raw_bundle())
    result = finalize_task_coverage(missing, QUESTIONS)[0]
    assert result["status"] == "not_assessed"
    assert result["reason"] == "coverage_assessment_missing"
    assert QUESTION in missing.coverage_gaps[0].description


def test_unanswered_is_valid_without_any_published_record():
    bundle = validated(raw_bundle([assessment("unanswered")]))
    bundle.facts = []
    result = finalize_task_coverage(bundle, QUESTIONS)[0]
    assert result["status"] == "unanswered"
    assert result["answer_claim_ids"] == []
    assert "覆盖状态为未回答" in bundle.coverage_gaps[0].description


@pytest.mark.parametrize("decision", ["skip", "link_only"])
def test_discarded_source_cannot_answer_via_retained_audit_objects(decision):
    bundle = validated(raw_bundle([assessment("covered")]))
    bundle.decision = decision
    assert bundle.facts  # these objects may survive for audit even though not admitted
    result = finalize_task_coverage(bundle, QUESTIONS)[0]
    assert result["status"] == "not_assessed"
    assert result["reason"] == "coverage_answer_not_published"


@pytest.mark.parametrize("event_status,keep_fact,expected", [
    ("pending", False, "not_assessed"),
    (None, False, "not_assessed"),
    ("resolved", False, "covered"),
    ("pending", True, "covered"),
])
def test_event_answer_uses_shared_resolution_without_discarding_independent_facts(event_status, keep_fact, expected):
    text = "公司已发布新款设备。"
    raw = raw_bundle([assessment("covered")], text=text)
    raw["content_review"]["claims"][0]["field_values"] = {"event_type": "product_launch"}
    raw["content_review"]["bindings"]["/events/0"] = ["answer"]
    raw["events"] = [{"event_type": "product_launch", "summary": text,
                      "evidence_locator": {"passage_id": "p1"}}]
    if not keep_fact:
        raw["facts"] = []
    bundle = validated(raw, text=text)
    assert len(bundle.events) == 1
    # A fixture supplies the shared event review result, not an independent semantic rule.
    bundle.events[0].event_resolution = {"status": event_status} if event_status else {}
    result = finalize_task_coverage(bundle, QUESTIONS)[0]
    assert result["status"] == expected
    if expected == "not_assessed":
        assert result["reason"] == "coverage_answer_not_published"
    else:
        assert result["answer_claim_ids"] == ["answer"]
    assert len(bundle.events) == 1
    assert bool(bundle.facts) is keep_fact


@pytest.mark.parametrize("rows,reason", [
    ([], "coverage_assessment_missing"),
    ([assessment(), assessment()], "coverage_assessment_duplicate"),
    ([{**assessment(), "question_id": "unrequested"}], "coverage_question_id_invalid"),
    ([{**assessment(), "question_id": {}}], "coverage_question_id_invalid"),
    ({"q1": assessment()}, "coverage_question_id_invalid"),
    ([None], "coverage_question_id_invalid"),
    ([{**assessment(), "status": {}}], "coverage_assessment_invalid"),
    ([assessment("covered", answers=[])], "coverage_assessment_invalid"),
    ([assessment("covered", aspects=["经营"])], "coverage_assessment_invalid"),
    ([assessment("partial", aspects=[])], "coverage_assessment_invalid"),
    ([assessment("unanswered", answers=["answer"])], "coverage_assessment_invalid"),
    ([assessment(aspects=["该公司没有融资活动"])], "coverage_assessment_invalid"),
    ([assessment(answers=["answer", "answer"])], "coverage_assessment_invalid"),
    ([assessment(answers=["absent"])], "coverage_answer_not_published"),
])
def test_invalid_assessments_fall_back_without_publishing_model_assertions(rows, reason):
    bundle = validated(raw_bundle(rows))
    result = finalize_task_coverage(bundle, QUESTIONS)[0]
    assert result["status"] == "not_assessed"
    assert result["reason"] == reason
    assert result["answer_claim_ids"] == result["uncovered_aspects"] == []
    assert bundle.coverage_gaps[0].description == f"本来源材料尚未完成以下问题的覆盖评估：{QUESTION}"


@pytest.mark.parametrize("mutation", ["removed", "changed", "pending"])
def test_answer_must_survive_final_review_and_admission(mutation):
    raw = raw_bundle([assessment("covered")],
                     claim_status="pending_review" if mutation == "pending" else "source_supported")
    bundle = validated(raw)
    if mutation == "removed":
        bundle.facts = []
    if mutation == "changed":
        bundle.facts[0].fact_statement = "未经审核的新说法"
    result = finalize_task_coverage(bundle, QUESTIONS)[0]
    assert result["status"] == "not_assessed"
    assert result["reason"] == "coverage_answer_not_published"


def test_same_claim_id_in_another_contribution_cannot_authorize_an_answer():
    retained = validated(raw_bundle([assessment("covered")]))
    other_text = "来源仅介绍一个仍在规划中的项目。"
    other = validated(raw_bundle([assessment("covered")], text=other_text), text=other_text)
    retained.content_review = {"protocol": PROTOCOL, "status": "applied",
                               "contributions": [retained.content_review, other.content_review]}
    result = finalize_task_coverage(retained, QUESTIONS)[0]
    assert result["status"] == "not_assessed"
    assert result["assessments"][0]["status"] == "covered"
    assert result["assessments"][1]["reason"] == "coverage_answer_not_published"


def test_conflicting_contributions_do_not_upgrade_coverage():
    bundle = validated(raw_bundle([assessment("covered")]))
    other = validated(raw_bundle([assessment("unanswered")]))
    bundle.content_review = {"protocol": PROTOCOL, "status": "applied", "contributions": [
        {"protocol": PROTOCOL, "contributions": [bundle.content_review]}, other.content_review]}
    result = finalize_task_coverage(bundle, QUESTIONS)[0]
    assert result["status"] == "not_assessed"
    assert result["reason"] == "coverage_assessments_conflict"
    assert len(result["assessments"]) == 2


def test_repeated_finalization_replaces_only_generated_gaps():
    bundle = validated(raw_bundle([assessment()]))
    existing = CoverageGap(gap_type="missing_date", description="来源未注明披露日期。")
    bundle.coverage_gaps = [existing]
    first = finalize_task_coverage(bundle, QUESTIONS)
    assert finalize_task_coverage(bundle, QUESTIONS) == first
    assert len(bundle.coverage_gaps) == 2
    bundle.content_review["model_review"]["task_coverage"] = [assessment("covered")]
    assert finalize_task_coverage(bundle, QUESTIONS)[0]["status"] == "covered"
    assert bundle.coverage_gaps == [existing]


def test_empty_or_cached_unassessed_bundle_retains_all_questions_without_truncation():
    questions = [{"question_id": f"q{index}", "question": f"需求{index}"} for index in range(12)]
    bundle = BundleExtraction()
    result = finalize_task_coverage(bundle, questions)
    assert len(result) == len(bundle.coverage_gaps) == 12
    assert all(row["status"] == "not_assessed" and row["source_link_id"] is None for row in result)
    assert all(row["scope"] == "run_material" for row in result)
    assert all(gap.description.startswith("本轮已读材料尚未完成") for gap in bundle.coverage_gaps)
    assert not bundle.facts and not bundle.metrics and not bundle.events


def test_invalid_caller_question_contract_is_rejected_before_mutation():
    bundle = BundleExtraction()
    before = deepcopy(bundle.model_dump())
    with pytest.raises(ValueError):
        finalize_task_coverage(bundle, QUESTIONS + QUESTIONS)
    assert bundle.model_dump() == before


def comparison_bundle(comparison, *, status="source_supported", binding=True):
    rate = comparison.get("yoy")
    percent = rate * 100 if rate is not None else None
    text = (f"本季度交付12台，同比增长{percent:g}%；另一项比较不适用。"
            if percent is not None else "本季度交付12台，比较不适用。")
    raw = raw_bundle([assessment(answers=["metric", "comparison"])], text=text)
    observation = {"id": "metric", "kind": "observation", "status": "source_supported",
                   "statement": "本季度交付12台。", "reason": "数量与口径由原文支持。",
                   "evidence": [{"passage_id": "p1", "quote": text}], "depends_on": []}
    comparable = {"id": "comparison", "kind": "comparability", "status": status,
                  "statement": "来源给出交付数量的比较情况。", "reason": "比较判断由原文支持。",
                  "evidence": [{"passage_id": "p1", "quote": text}], "depends_on": ["metric"],
                  "field_values": {"comparison": comparison}}
    if rate is not None:
        comparable["field_values"]["comparison_input"] = {"yoy": {
            "value": percent, "unit": "percent",
            "evidence": [{"passage_id": "p1", "quote": f"同比增长{percent:g}%"}]}}
    raw["metrics"] = [{"metric_name": "交付数量", "metric_value": 12, "unit": "台",
                       "comparison": comparison, "evidence_locator": {"passage_id": "p1"}}]
    raw["content_review"]["claims"].extend([observation, comparable])
    raw["content_review"]["bindings"]["/metrics/0"] = ["metric"]
    if binding:
        raw["content_review"]["bindings"]["/metrics/0/comparison"] = ["comparison"]
    return raw, text


@pytest.mark.parametrize("comparison", [{"yoy": .2}, {"yoy": 0},
                                         {"yoy": None, "yoy_status": "not_applicable"}])
def test_published_comparison_can_support_task_coverage(comparison):
    raw, text = comparison_bundle(comparison)
    bundle = validated(raw, text=text)
    assert bundle.metrics[0].comparison == comparison
    result = finalize_task_coverage(bundle, QUESTIONS)[0]
    assert result["status"] == "partial"
    assert result["answer_claim_ids"] == ["metric", "comparison"]
    assert result["uncovered_aspects"] == ["经营", "融资进展"]


@pytest.mark.parametrize("comparison", [{}, {"yoy": None}])
def test_empty_comparison_claim_is_not_a_published_answer(comparison):
    raw, text = comparison_bundle(comparison)
    bundle = validated(raw, text=text)
    result = finalize_task_coverage(bundle, QUESTIONS)[0]
    assert result["reason"] == "coverage_answer_not_published"
    assert result["status"] == "not_assessed"
    assert bundle.metrics[0].metric_value == 12


@pytest.mark.parametrize("status", ["pending_review", "inference", "unsupported"])
def test_held_comparison_cannot_borrow_the_approved_metric(status):
    raw, text = comparison_bundle({"yoy": .2}, status=status)
    bundle = validated(raw, text=text)
    assert bundle.metrics[0].comparison == {}
    assert finalize_task_coverage(bundle, QUESTIONS)[0]["reason"] == "coverage_answer_not_published"


def test_unbound_or_modified_auxiliary_claim_cannot_support_coverage():
    raw, text = comparison_bundle({"yoy": .2}, binding=False)
    bundle = validated(raw, text=text)
    assert bundle.metrics[0].comparison == {}
    assert finalize_task_coverage(bundle, QUESTIONS)[0]["reason"] == "coverage_answer_not_published"
    raw, text = comparison_bundle({"yoy": .2})
    bundle = validated(raw, text=text)
    bundle.metrics[0].comparison["yoy"] = .9
    assert finalize_task_coverage(bundle, QUESTIONS)[0]["reason"] == "coverage_answer_not_published"


def test_nested_comparison_binding_can_authorize_only_its_published_field():
    raw, text = comparison_bundle({"yoy": .2})
    bindings = raw["content_review"]["bindings"]
    bindings["/metrics/0/comparison/yoy"] = bindings.pop("/metrics/0/comparison")
    bundle = validated(raw, text=text)
    assert bundle.metrics[0].content_review["fields"]["comparison/yoy"]["status"] == "source_supported"
    assert finalize_task_coverage(bundle, QUESTIONS)[0]["status"] == "partial"


def test_auxiliary_answer_preserves_contribution_provenance():
    raw, text = comparison_bundle({"yoy": .2})
    retained = validated(raw, text=text)
    other_raw, other_text = comparison_bundle({"yoy": .9})
    other = validated(other_raw, text=other_text)
    retained.content_review = {"protocol": PROTOCOL, "status": "applied", "contributions": [
        retained.content_review, other.content_review]}
    result = finalize_task_coverage(retained, QUESTIONS)[0]
    assert result["status"] == "not_assessed"
    assert result["assessments"][0]["status"] == "partial"
    assert result["assessments"][1]["reason"] == "coverage_answer_not_published"
