"""Offline regression for source-supported CNY scale normalization."""
import copy
import unittest

from mic.schemas import Passage
from mic.validate import BundleValidator

CATL = "二标段10MW/40MWh钠离子储能系统，宁德时代中标价为4141.622万元，合单价1.035元/Wh。"
ENVISION = "30MW/120MWh+60MW/240MWh磷酸铁锂，远景能源中标价19461.6万元，合单价0.518元/Wh。"


def check_amount(value, text=CATL, **fields):
    raw = {"events": [{"entities": {"subject": "宁德时代"},
                       "metrics": {"amount": value, "currency": "CNY", **fields},
                       "evidence_locator": {"passage_id": "p1"}}]}
    result = BundleValidator({}).validate(raw, [Passage(passage_id="p1", section="正文", text=text)])
    assert result.schema_valid
    return result.bundle.events[0].metrics, result.warnings


class MoneyTests(unittest.TestCase):
    def test_real_catl_amount(self):
        amount, warnings = check_amount(4141.622)
        self.assertEqual(amount["amount"], 41416220)
        self.assertEqual(amount["amount_unit"], "元")
        self.assertEqual((amount["amount_raw"], amount["amount_raw_unit"]), (4141.622, "万元"))
        self.assertEqual(amount["amount_evidence"]["quote"], "4141.622万元")
        self.assertFalse(warnings)

    def test_real_envision_amount(self):
        amount, _ = check_amount(19461.6, ENVISION)
        self.assertEqual(amount["amount"], 194616000)

    def test_yuan_input_does_not_multiply_again(self):
        amount, _ = check_amount(41416220)
        self.assertEqual(amount["amount"], 41416220)

    def test_explicit_unit(self):
        amount, _ = check_amount(1.2, "订单金额为1.2亿元。", amount_unit="亿元")
        self.assertEqual(amount["amount"], 120000000)

    def test_model_unit_must_match_evidence(self):
        amount, warnings = check_amount(4141.622, amount_unit="亿元")
        self.assertEqual(amount["amount"], 4141.622)
        self.assertTrue(any("not_supported" in x for x in warnings))

    def test_different_number_not_silently_corrected(self):
        amount, warnings = check_amount(4141.623)
        self.assertEqual(amount["amount"], 4141.623)
        self.assertTrue(any("not_supported" in x for x in warnings))

    def test_ambiguous_scale_not_guessed(self):
        amount, warnings = check_amount(1, "甲中标1亿元，乙中标1万元。")
        self.assertEqual(amount["amount"], 1)
        self.assertTrue(any("ambiguous_scale" in x for x in warnings))

    def test_already_scaled_or_raw_ambiguity(self):
        amount, warnings = check_amount(10000, "甲中标1万元，乙中标10000万元。")
        self.assertEqual(amount["amount"], 10000)
        self.assertTrue(any("ambiguous_scale" in x for x in warnings))

    def test_price_is_not_contract_amount(self):
        amount, warnings = check_amount(1.035, "单价1.035元/Wh。")
        self.assertEqual(amount["amount"], 1.035)
        self.assertTrue(any("not_supported" in x for x in warnings))

    def test_currency_not_guessed(self):
        for currency in (None, "USD"):
            with self.subTest(currency=currency):
                amount, warnings = check_amount(4141.622, currency=currency)
                self.assertEqual(amount["amount"], 4141.622)
                self.assertTrue(any("currency" in x for x in warnings))

    def test_mixed_currency_passage(self):
        amount, warnings = check_amount(20, "合同20万元，另有USD 10万元。")
        self.assertEqual(amount["amount"], 20)
        self.assertTrue(any("mixed_currency" in x for x in warnings))

    def test_comma_number_and_noninteger_yuan(self):
        amount, _ = check_amount(1234.56789, "金额1,234.56789万元。")
        self.assertEqual(amount["amount"], 12345678.9)

    def test_invalid_passage_cannot_normalize(self):
        raw = {"events": [{"metrics": {"amount": 4141.622, "currency": "CNY"},
                           "evidence_locator": {"passage_id": "not-in-input"}}]}
        result = BundleValidator({}).validate(raw, [Passage(passage_id="p1", section="正文", text=CATL)])
        self.assertEqual(result.bundle.events[0].metrics["amount"], 4141.622)
        self.assertTrue(any("missing_cited_passage" in x for x in result.warnings))

    def test_original_preserved_and_revalidation_is_amount_idempotent(self):
        raw = {"facts": [{"metrics": {"amount": 4141.622, "currency": "CNY", "unit": "吨"},
                          "evidence_locator": {"passage_id": "p1"}}]}
        original = copy.deepcopy(raw)
        passages = [Passage(passage_id="p1", section="正文", text=CATL)]
        first = BundleValidator({}).validate(raw, passages)
        second = BundleValidator({}).validate(first.bundle.model_dump(), passages)
        self.assertEqual(raw, original)
        self.assertEqual(first.bundle.facts[0].metrics, second.bundle.facts[0].metrics)
        self.assertEqual(second.bundle.facts[0].metrics["unit"], "吨")
        self.assertFalse(first.warnings)
        self.assertFalse(second.warnings)

    def test_relation_with_explicit_currency(self):
        raw = {"relations": [{"subject_entity": {"name": "宁德时代"},
                              "relation_type": "supplier_of",
                              "object_entity": {"name": "甲公司"},
                              "qualifiers": {"amount": 4141.622, "currency": "CNY"},
                              "evidence_locator": {"passage_id": "p1"}}]}
        result = BundleValidator({}).validate(raw, [Passage(
            passage_id="p1", section="正文",
            text="宁德时代是甲公司的供应商，供货金额4141.622万元。")])
        self.assertEqual(result.bundle.relations[0].qualifiers["amount"], 41416220)


if __name__ == "__main__":
    unittest.main(verbosity=2)
