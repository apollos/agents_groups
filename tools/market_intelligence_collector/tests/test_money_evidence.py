"""Offline regression for amount format checks and Decimal conversion.

Source support for an amount is a model judgment enforced by the shared content
review; this module only checks structure, citation membership, record/claim
agreement and the arithmetic. Nothing here searches passage text for numbers.
"""
import copy
import unittest

from mic.money import normalize_bundle_amounts, normalize_cny_fields, reason_status
from mic.schemas import Passage
from mic.validate import BundleValidator

CATL = "二标段10MW/40MWh钠离子储能系统，宁德时代中标价为4141.622万元，合单价1.035元/Wh。"
TABLE = ("（三）主要会计数据和财务指标\n□是 ■否\n单位：千元\n项目 2025 年 2024 年 本年比上年增减\n"
         "营业收入 423,701,834 362,012,554 17.04% 400,917,045")
PASSAGES = {"p1": CATL, "t1": TABLE}


def spec(value=4141.622, unit="万元", currency="CNY", evidence=None):
    return {"currency": currency, "value": value, "unit": unit,
            "evidence": evidence or {"passage_id": "p1", "quote": f"{value}{unit}"}}


class ReviewedAmountTests(unittest.TestCase):
    """normalize_cny_fields applies a claim.amount that content review already accepted."""

    def test_wan_yuan_claim_is_converted_with_evidence(self):
        fields = {"amount": 4141.622, "currency": "CNY"}
        out, reason = normalize_cny_fields(fields, PASSAGES, [spec()])
        self.assertEqual(reason, "supported")
        self.assertEqual((out["amount"], out["currency"], out["amount_unit"]), (41416220, "CNY", "元"))
        self.assertEqual((out["amount_raw"], out["amount_raw_unit"]), (4141.622, "万元"))
        self.assertEqual(out["amount_evidence"], {"passage_id": "p1", "quote": "4141.622万元"})
        self.assertEqual(out["amount_input"], fields)

    def test_thousand_yuan_table_value_with_split_citations(self):
        evidence = [{"passage_id": "t1", "quote": "营业收入 423,701,834 362,012,554 17.04%"},
                    {"passage_id": "t1", "quote": "单位：千元"}]
        fields = {"amount": 423701834, "currency": "CNY", "amount_unit": "千元"}
        out, reason = normalize_cny_fields(fields, PASSAGES, [spec(423701834, "千元", evidence=evidence)])
        self.assertEqual(reason, "supported")
        self.assertEqual((out["amount"], out["currency"], out["amount_unit"]), (423701834000, "CNY", "元"))
        self.assertEqual((out["amount_raw"], out["amount_raw_unit"]), (423701834, "千元"))
        self.assertEqual(out["amount_evidence"], evidence)
        self.assertNotIn("amount_conversion", out)

    def test_currency_scale_syntax_in_record_is_repaired_not_rejected(self):
        for currency, unit in (("CNY万元", None), ("CNY", "万元"), ("RMB万元", None)):
            with self.subTest(currency=currency):
                fields = {"amount": 4141.622, "currency": currency}
                if unit:
                    fields["amount_unit"] = unit
                out, reason = normalize_cny_fields(fields, PASSAGES, [spec()])
                self.assertEqual(reason, "supported")
                self.assertEqual(out["amount"], 41416220)
                self.assertEqual(out["amount_input"], fields)

    def test_already_converted_record_is_not_multiplied_again(self):
        out, reason = normalize_cny_fields({"amount": 41416220, "currency": "CNY", "amount_unit": "元"}, PASSAGES, [spec()])
        self.assertEqual((reason, out["amount"], out["amount_raw"]), ("supported", 41416220, 4141.622))

    def test_unknown_unit_is_kept_unconverted_with_a_note(self):
        evidence = {"passage_id": "p1", "quote": "4141.622"}
        out, reason = normalize_cny_fields({"amount": 4141.622, "currency": "CNY", "amount_unit": "万日元折合"},
                                           PASSAGES, [spec(unit="万日元折合", evidence=evidence)])
        self.assertEqual(reason, "supported")
        self.assertEqual((out["amount"], out["amount_unit"], out["amount_raw"]), (4141.622, "万日元折合", 4141.622))
        self.assertEqual(out["amount_conversion"], {"status": "not_converted", "reason": "unknown_amount_unit"})
        self.assertEqual(out["amount_evidence"], evidence)

    def test_foreign_currency_is_kept_as_given(self):
        evidence = {"passage_id": "p1", "quote": "4141.622"}
        out, reason = normalize_cny_fields({"amount": 4141.622, "currency": "USD", "amount_unit": "万元"},
                                           PASSAGES, [spec(currency="USD", evidence=evidence)])
        self.assertEqual(reason, "supported")
        self.assertEqual((out["amount"], out["currency"], out["amount_unit"]), (4141.622, "USD", "万元"))
        self.assertEqual(out["amount_conversion"]["reason"], "non_cny_currency")

    def test_missing_claim_amount_is_pending_review_not_format(self):
        out, reason = normalize_cny_fields({"amount": 4141.622, "currency": "CNY"}, PASSAGES, [])
        self.assertIsNone(out)
        self.assertEqual((reason, reason_status(reason)), ("amount_claim_missing", "pending_review"))

    def test_citation_must_belong_to_the_input(self):
        for evidence in ({"passage_id": "p1", "quote": "原文没有这一句"},
                         {"passage_id": "title", "quote": "4141.622万元"},
                         {"passage_id": "missing", "quote": "4141.622万元"},
                         [{"passage_id": "p1", "quote": "4141.622万元"}, {"passage_id": "p1", "quote": "不在正文"}]):
            with self.subTest(evidence=evidence):
                out, reason = normalize_cny_fields({"amount": 4141.622, "currency": "CNY"}, PASSAGES, [spec(evidence=evidence)])
                self.assertIsNone(out)
                self.assertEqual((reason, reason_status(reason)), ("amount_citation_unverified", "pending_review"))

    def test_claim_cannot_replace_the_record_number_currency_or_scale(self):
        cases = [({"amount": 123, "currency": "CNY"}, [spec()], "normalization_value_conflict"),
                 ({"amount": 4141.622, "currency": "USD"}, [spec()], "normalization_currency_conflict"),
                 ({"amount": 4141.622, "currency": "CNY", "amount_unit": "亿元"}, [spec()], "normalization_scale_conflict"),
                 ({"amount": 4141.622, "currency": "CNY万元", "amount_unit": "亿元"}, [spec()], "conflicting_currency_unit"),
                 ({"amount": 4141.622, "currency": "CNY"}, [spec(), spec(unit="亿元", evidence={"passage_id": "p1", "quote": "4141.622"})],
                  "amount_claims_conflict"),
                 ({"amount": 4141.622, "currency": "CNY"}, [{"currency": "CNY", "value": "abc", "unit": "万元",
                                                              "evidence": {"passage_id": "p1", "quote": "4141.622万元"}}],
                  "amount_structure_invalid"),
                 ({"amount": "n/a", "currency": "CNY"}, [spec()], "invalid_amount")]
        for fields, specs, expected in cases:
            with self.subTest(expected=expected):
                out, reason = normalize_cny_fields(fields, PASSAGES, specs)
                self.assertIsNone(out)
                self.assertEqual((reason, reason_status(reason)), (expected, "format_pending"))

    def test_no_passage_search_and_no_scale_guessing(self):
        # Passage text is irrelevant once the claim is accepted: only its quotes are checked.
        quiet = {"p1": "4141.622万元"}
        out, reason = normalize_cny_fields({"amount": 4141.622, "currency": "CNY"}, quiet, [spec()])
        self.assertEqual((reason, out["amount"]), ("supported", 41416220))
        mixed = {"p1": "甲中标1亿元，乙中标1万元，另有USD 10万元；4141.622万元"}
        out, reason = normalize_cny_fields({"amount": 4141.622, "currency": "CNY"}, mixed, [spec()])
        self.assertEqual((reason, out["amount"]), ("supported", 41416220))


def check_amount(value, text=CATL, **fields):
    raw = {"events": [{"entities": {"subject": "宁德时代"},
                       "metrics": {"amount": value, "currency": "CNY", **fields},
                       "evidence_locator": {"passage_id": "p1"}}]}
    result = BundleValidator({}, require_content_review=False).validate(raw, [Passage(passage_id="p1", section="正文", text=text)])
    assert result.schema_valid
    return result.bundle.events[0].metrics, result.warnings


class LegacyPathTests(unittest.TestCase):
    """Without content review there is no evidence to apply: unit arithmetic only."""

    def test_explicit_unit_is_converted(self):
        for unit, expected in (("万元", 41416220), ("千元", 4141622), ("亿元", 414162200000)):
            with self.subTest(unit=unit):
                amount, warnings = check_amount(4141.622, amount_unit=unit)
                self.assertEqual((amount["amount"], amount["amount_unit"]), (expected, "元"))
                self.assertEqual((amount["amount_raw"], amount["amount_raw_unit"]), (4141.622, unit))
                self.assertFalse(warnings)

    def test_currency_scale_syntax(self):
        amount, _ = check_amount(1.2, currency="CNY亿元")
        self.assertEqual(amount["amount"], 120000000)

    def test_missing_unit_is_not_guessed_from_text(self):
        amount, warnings = check_amount(4141.622)
        self.assertEqual(amount["amount"], 4141.622)
        self.assertNotIn("amount_raw", amount)
        self.assertTrue(any("missing_amount_unit" in x for x in warnings))

    def test_text_is_never_used_to_reject(self):
        amount, warnings = check_amount(20, "合同20万元，另有USD 10万元。", amount_unit="万元")
        self.assertEqual(amount["amount"], 200000)
        self.assertFalse(warnings)

    def test_non_cny_or_unknown_unit_left_unchanged(self):
        amount, warnings = check_amount(4141.622, currency="USD", amount_unit="万元")
        self.assertEqual((amount["amount"], amount["currency"]), (4141.622, "USD"))
        self.assertTrue(any("non_cny_currency" in x for x in warnings))
        amount, warnings = check_amount(4141.622, amount_unit="万美元")
        self.assertEqual(amount["amount"], 4141.622)
        self.assertTrue(any("unknown_amount_unit" in x for x in warnings))

    def test_comma_number_and_noninteger_yuan(self):
        amount, _ = check_amount("1,234.56789", amount_unit="万元")
        self.assertEqual(amount["amount"], 12345678.9)

    def test_original_preserved_and_revalidation_is_amount_idempotent(self):
        raw = {"facts": [{"metrics": {"amount": 4141.622, "currency": "CNY", "amount_unit": "万元", "unit": "吨"},
                          "evidence_locator": {"passage_id": "p1"}}]}
        original = copy.deepcopy(raw)
        passages = [Passage(passage_id="p1", section="正文", text=CATL)]
        first = BundleValidator({}, require_content_review=False).validate(raw, passages)
        second = BundleValidator({}, require_content_review=False).validate(first.bundle.model_dump(), passages)
        self.assertEqual(raw, original)
        self.assertEqual(first.bundle.facts[0].metrics, second.bundle.facts[0].metrics)
        self.assertEqual(second.bundle.facts[0].metrics["unit"], "吨")
        self.assertFalse(first.warnings)
        self.assertFalse(second.warnings)

    def test_relation_with_explicit_currency(self):
        raw = {"relations": [{"subject_entity": {"name": "宁德时代"},
                              "relation_type": "supplier_of",
                              "object_entity": {"name": "甲公司"},
                              "qualifiers": {"amount": 4141.622, "currency": "CNY万元"},
                              "evidence_locator": {"passage_id": "p1"}}]}
        result = BundleValidator({}, require_content_review=False).validate(raw, [Passage(
            passage_id="p1", section="正文", text="宁德时代是甲公司的供应商，供货金额4141.622万元。")])
        self.assertEqual(result.bundle.relations[0].qualifiers["amount"], 41416220)
        warnings = []
        normalize_bundle_amounts(result.bundle, warnings)
        self.assertEqual(result.bundle.relations[0].qualifiers["amount"], 41416220)
        self.assertFalse(warnings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
