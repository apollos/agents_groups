"""Offline evidence-gate regressions; no network or model clients."""
import copy
import unittest

from mic.schemas import Passage
from mic.validate import BundleValidator


def candidate(kind="supplier_of", pid="p1", subject="甲公司", obj="乙公司"):
    return {"relation_type": kind, "subject_entity": {"name": subject},
            "object_entity": {"name": obj}, "qualifiers": {"status": "confirmed"},
            "evidence_locator": {"passage_id": pid}, "confidence": 0.9}


def validate(relation, text, *, pid="p1", section="正文", extra=()):
    return BundleValidator({}, require_content_review=False).validate(
        {"relations": [relation]},
        [Passage(passage_id=pid, section=section, text=text), *extra])


class RelationEvidenceTests(unittest.TestCase):
    def assert_pending(self, report, reason):
        self.assertTrue(report.schema_valid)
        self.assertEqual(report.bundle.relations, [])
        self.assertEqual(len(report.relation_reviews), 1)
        self.assertIn(reason, report.relation_reviews[0]["reason_codes"])
        self.assertEqual(report.relation_reviews[0]["status"], "pending_evidence")
        self.assertTrue(any(g.gap_type == "relation_evidence_unverified"
                            for g in report.bundle.coverage_gaps))
        self.assertTrue(any("relation evidence quarantined" in w for w in report.warnings))

    def test_title_co_mention_is_not_competition(self):
        report = validate(candidate("competitor_of", "title"),
                          "甲公司、乙公司分别中标两个标段", pid="title", section="标题")
        self.assert_pending(report, "title_only_evidence")

    def test_title_section_cannot_be_bypassed_by_body_pid(self):
        report = validate(candidate("competitor_of"), "甲公司与乙公司是竞争对手。", section="文章标题")
        self.assert_pending(report, "title_only_evidence")

    def test_missing_or_invalid_or_empty_evidence(self):
        for pid, text in ((None, "甲公司是乙公司的供应商。"),
                          ("missing", "甲公司是乙公司的供应商。"), ("p1", "  ")):
            with self.subTest(pid=pid, text=text):
                self.assert_pending(validate(candidate(pid=pid), text), "missing_body_evidence")

    def test_no_passages_does_not_bypass_gate(self):
        self.assert_pending(BundleValidator({}, require_content_review=False).validate({"relations": [candidate()]}, []),
                            "missing_body_evidence")

    def test_duplicate_passage_ids_are_ambiguous(self):
        report = validate(candidate(), "甲公司是乙公司的供应商。", extra=[
            Passage(passage_id="p1", section="正文", text="完全不同的正文。")])
        self.assert_pending(report, "duplicate_passage_id")

    def test_project_name_does_not_identify_owner(self):
        relation = candidate(subject="宁德时代", obj="河北任丘智弘（独立储能试点项目业主）")
        self.assert_pending(validate(relation, "宁德时代中标10MW/40MWh钠离子储能系统。"),
                            "object_not_literal")

    def test_other_passage_does_not_repair_missing_entity(self):
        report = validate(candidate(), "甲公司中标二标段。", extra=[
            Passage(passage_id="p2", section="正文", text="乙公司在另一项目中采购设备。")])
        self.assert_pending(report, "object_not_literal")

    def test_missing_subject_and_alias_not_guessed(self):
        self.assert_pending(validate(candidate(subject="甲股份有限公司"),
                                     "甲公司是乙公司的供应商。"), "subject_not_literal")

    def test_body_co_mention_does_not_establish_competition(self):
        report = validate(candidate("competitor_of"), "甲公司中标一标段，乙公司中标二标段。")
        self.assert_pending(report, "competitor_statement_unverified")

    def test_another_pairs_predicate_cannot_be_borrowed(self):
        self.assert_pending(validate(candidate("competitor_of"),
                                     "甲公司、乙公司参与会议。丙公司与丁公司是竞争对手。"),
                            "competitor_statement_unverified")

    def test_negative_conditional_rumored_and_quoted_competition(self):
        cases = ["甲公司与乙公司不是竞争对手。", "甲公司与乙公司可能是竞争对手。",
                 "如果甲公司与乙公司是竞争对手。", "甲公司与乙公司是竞争对手？",
                 "“甲公司与乙公司是竞争对手”的说法不实。",
                 "甲公司与乙公司是竞争对手。该说法已被否认。",
                 "过去甲公司与乙公司是竞争对手。"]
        for text in cases:
            with self.subTest(text=text):
                self.assert_pending(validate(candidate("competitor_of"), text),
                                    "competitor_statement_unverified")

    def test_explicit_competitor_pair_retained_without_status_upgrade(self):
        for text in ("甲公司与乙公司是竞争对手。", "乙公司是甲公司的直接竞争对手。",
                     "甲公司和乙公司存在直接竞争关系。"):
            with self.subTest(text=text):
                relation = candidate("competitor_of")
                relation["qualifiers"]["status"] = "new"
                report = validate(relation, text)
                self.assertEqual(len(report.bundle.relations), 1)
                self.assertEqual(report.relation_reviews, [])
                self.assertEqual(report.bundle.relations[0].qualifiers["status"], "new")

    def test_explicit_supplier_kept_with_direction_intact(self):
        report = validate(candidate(), "甲公司是乙公司的供应商。")
        self.assertEqual(len(report.bundle.relations), 1)
        self.assertEqual(report.bundle.relations[0].relation_type, "supplier_of")
        self.assertEqual(report.bundle.relations[0].subject_entity.name, "甲公司")

    def test_unknown_type_quarantined(self):
        self.assert_pending(validate(candidate("made_up"), "甲公司与乙公司有往来。"),
                            "unknown_relation_type")

    def test_original_data_event_amount_and_existing_gap_preserved(self):
        raw = {"relations": [candidate("competitor_of", "title")],
               "events": [{"entities": {"subject": "宁德时代"},
                           "metrics": {"amount": 4141.622, "currency": "CNY", "amount_unit": "万元"},
                           "evidence_locator": {"passage_id": "p3"}}],
               "coverage_gaps": [{"gap_type": "missing_date", "description": "日期待确认"}]}
        before = copy.deepcopy(raw)
        passages = [Passage(passage_id="title", section="标题", text="甲公司、乙公司中标"),
                    Passage(passage_id="p3", section="二标段", text="宁德时代中标价4141.622万元。")]
        first = BundleValidator({}, require_content_review=False).validate(raw, passages)
        self.assertEqual(raw, before)
        self.assertEqual(first.relation_reviews[0]["candidate"]["qualifiers"]["status"], "confirmed")
        self.assertEqual(first.bundle.events[0].metrics["amount"], 41416220)
        second = BundleValidator({}, require_content_review=False).validate(first.bundle.model_dump(), passages)
        self.assertEqual(first.bundle.model_dump(), second.bundle.model_dump())
        self.assertEqual(len(second.bundle.coverage_gaps), 2)
        self.assertEqual(second.relation_reviews, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
