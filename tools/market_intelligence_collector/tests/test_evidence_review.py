"""Strict evidence review regression: observable values, provenance and isolation."""
import copy
import unittest

from mic.schemas import Passage
from mic.validate import BundleValidator


def run(raw, text="", extra=(), strict=True):
    passages = [Passage(passage_id="p1", section="正文", text=text), *extra]
    return BundleValidator({"strict_evidence_review": strict}).validate(raw, passages)


def price(value=1.035, unit="元/Wh", **fields):
    return {"facts": [{"fact_type": "price", "metrics": {
        "amount": value, "currency": "CNY", "unit": unit, **fields},
        "evidence_locator": {"passage_id": "p1"}}]}


class QualityTests(unittest.TestCase):
    def test_price_is_not_total_and_preserves_source(self):
        for value in (1.035, 0.518):
            with self.subTest(value=value):
                raw = price(value)
                before = copy.deepcopy(raw)
                report = run(raw, f"中标合单价{value}元/Wh。")
                fields = report.bundle.facts[0].metrics
                self.assertIsNone(fields["amount"])
                self.assertEqual(fields["unit_price"], value)
                self.assertEqual(fields["unit_price_evidence"]["quote"], f"{value}元/Wh")
                self.assertEqual(raw, before)

    def test_total_and_quantity_unit_are_unchanged(self):
        raw = {"facts": [{"fact_type": "order", "metrics": {
            "amount": 4141.622, "currency": "CNY", "volume": 40, "unit": "MWh"},
            "evidence_locator": {"passage_id": "p1"}}]}
        fields = run(raw, "中标金额4141.622万元，规模40MWh。").bundle.facts[0].metrics
        self.assertEqual(fields["amount"], 41416220)
        self.assertEqual(fields["unit"], "MWh")
        self.assertNotIn("unit_price", fields)

    def test_price_substring_is_not_a_match(self):
        fields = run(price(1.035), "报价11.035元/Wh。").bundle.facts[0].metrics
        self.assertIsNone(fields["amount"])
        self.assertNotIn("unit_price", fields)
        self.assertEqual(fields["amount_candidate"], 1.035)

    def test_missing_currency_cannot_be_filled(self):
        fields = run(price(currency=None), "合单价1.035元/Wh。").bundle.facts[0].metrics
        self.assertIsNone(fields["amount"])
        self.assertIsNone(fields["currency"])
        self.assertNotIn("unit_price", fields)

    def test_unsupported_amount_is_removed_from_canonical_field(self):
        raw = {"events": [{"metrics": {"amount": 999, "currency": "CNY"},
                           "evidence_locator": {"passage_id": "p1"}}]}
        report = run(raw, "实际合同金额100万元。")
        self.assertIsNone(report.bundle.events[0].metrics["amount"])
        self.assertEqual(report.bundle.events[0].metrics["amount_candidate"], 999)

    def test_false_unit_price_metadata_cannot_certify_value(self):
        raw = price()
        raw['facts'][0]['metrics'].update(amount=None, unit_price=999, unit_price_unit="元/Wh",
                                          unit_price_evidence={"quote": "999元/Wh"})
        fields = run(raw, "合单价1.035元/Wh。").bundle.facts[0].metrics
        self.assertIsNone(fields['unit_price'])

    def test_explicit_addition_is_supported_with_operands(self):
        raw = {"metrics": [{"metric_value": 360, "unit": "MWh", "confidence": .8,
                            "evidence_locator": {"passage_id": "p1"}}]}
        report = run(raw, "30MW/120MWh大容量磷酸铁锂+60MW/240MWh长寿命磷酸铁锂。")
        self.assertEqual(report.bundle.metrics[0].scope['value_evidence']['operands'], [120,240])
        self.assertEqual(report.bundle.metrics[0].confidence, .8)
        self.assertFalse(report.warnings)

    def test_arbitrary_or_subset_sums_not_accepted(self):
        cases = ["规模120MWh，另一项目240MWh。", "总计360MWh，其中120MWh+240MWh。",
                 "备选方案120MWh+240MWh。"]
        raw = {"metrics": [{"metric_value": 360, "unit": "MWh", "confidence": .8,
                            "evidence_locator": {"passage_id": "p1"}}]}
        for text in cases:
            with self.subTest(text=text):
                report = run(raw, text)
                if '总计360MWh' in text:
                    self.assertNotIn('value_evidence', report.bundle.metrics[0].scope)
                else:
                    self.assertEqual(report.bundle.metrics, [])
                    self.assertTrue(any(r['action'] == 'quarantine_metric' for r in report.quality_reviews))

    def test_quantity_unit_is_not_swapped(self):
        raw = {"metrics": [{"metric_value": 360, "unit": "MW", "confidence": .8,
                            "evidence_locator": {"passage_id": "p1"}}]}
        report = run(raw, "30MW/120MWh+60MW/240MWh。")
        self.assertEqual(report.bundle.metrics, [])
        self.assertTrue(any(r['action'] == 'quarantine_metric' for r in report.quality_reviews))

    def test_chinese_date_in_same_passage_is_valid(self):
        raw = {"events": [{"event_date": "2026-09-15", "confidence": .8,
                           "evidence_locator": {"passage_id": "p1"}}]}
        report = run(raw, "2026年9月15日发布中标公示。")
        self.assertEqual(report.bundle.events[0].event_date, "2026-09-15")
        self.assertEqual(report.bundle.events[0].confidence, .8)
        self.assertFalse(report.warnings)

    def test_date_from_other_passage_is_only_candidate(self):
        raw = {"events": [{"event_date": "2026-09-15", "evidence_locator": {"passage_id": "p1"}}]}
        report = run(raw, "公司中标。", extra=[Passage(passage_id='p2',section='正文',text='另一项目2026年9月15日公告。')])
        self.assertIsNone(report.bundle.events[0].event_date)
        self.assertEqual(report.bundle.events[0].metrics['event_date_review']['candidate_passages'][0]['passage_id'], 'p2')

    def test_wrong_role_counterparty_is_not_filled_from_other_passage(self):
        raw = {"events": [{"entities": {"subject": "甲公司", "counterparty": "乙公司"},
                           "evidence_locator": {"passage_id": "p1"}}]}
        report = run(raw, "甲公司中标。", extra=[Passage(passage_id='p2',section='正文',text='乙公司为其他项目供应商。')])
        self.assertIsNone(report.bundle.events[0].entities['counterparty'])
        self.assertEqual(report.bundle.events[0].entities['counterparty_candidate'], '乙公司')

    def test_quote_alone_does_not_prove_price_down_or_margin_pressure(self):
        raw = {"price_cost_margin_signals": [
            {"signal_type": kind,"value":.518,"evidence_locator":{"passage_id":"p1"}}
            for kind in ['product_price_down','margin_pressure','spread_change']]}
        report = run(raw, "中标合单价0.518元/Wh。")
        self.assertEqual(report.bundle.price_cost_margin_signals, [])
        self.assertEqual(len(report.quality_reviews),3)

    def test_explicit_price_down_statement_is_retained(self):
        raw = {"price_cost_margin_signals": [{"signal_type":"product_price_down","value":.8,
                                             "evidence_locator":{"passage_id":"p1"}}]}
        self.assertEqual(len(run(raw,"该产品单价从1元/Wh降至0.8元/Wh。").bundle.price_cost_margin_signals),1)

    def test_negated_trend_and_share_without_baseline_go_to_review(self):
        raw = {"price_cost_margin_signals":[{"signal_type":"product_price_down","evidence_locator":{"passage_id":"p1"}}]}
        self.assertFalse(run(raw,"该产品单价并未下降。").bundle.price_cost_margin_signals)
        raw = {"customer_supplier_signals":[{"signal_type":"share_change","customer_or_supplier":"甲公司",
                                             "evidence_locator":{"passage_id":"p1"}}]}
        self.assertFalse(run(raw,"甲公司中标一标段。").bundle.customer_supplier_signals)

    def test_policy_needs_named_issuer_and_policy_action(self):
        raw = {"policy_signals":[{"issuer":"能源局","policy_type":"industry_plan","evidence_locator":{"passage_id":"p1"}}]}
        self.assertFalse(run(raw,"能源局参观示范项目。").bundle.policy_signals)
        self.assertEqual(len(run(raw,"能源局印发储能行业规划。").bundle.policy_signals),1)

    def test_unsupported_cost_interpretation_becomes_review(self):
        raw = {"metrics":[{"metric_value":1.035,"unit":"元/Wh","interpretation":"反映成本高，挤压毛利。",
                            "evidence_locator":{"passage_id":"p1"}}]}
        report=run(raw,"报价1.035元/Wh。")
        self.assertEqual(report.bundle.metrics[0].metric_value,1.035)
        self.assertEqual(report.bundle.metrics[0].interpretation,"来源报价观察；经济含义待核查。")
        self.assertEqual(report.quality_reviews[0]['action'],'clear_interpretation')

    def test_source_basis_conflict_is_flagged_never_repriced(self):
        raw={"metrics":[{"metric_value":.518,"unit":"元/Wh","evidence_locator":{"passage_id":"p1"}}]}
        text="30MW/120MWh大容量+60MW/240MWh长寿命，甲公司中标价为19461.6万元，合单价0.518元/Wh。"
        report=run(raw,text)
        self.assertEqual(report.bundle.metrics[0].metric_value,.518)
        detail=report.bundle.metrics[0].scope['source_price_basis_review']
        self.assertEqual(detail['computed_price_yuan_per_Wh'],'0.5406')
        self.assertEqual(sum(g.gap_type=='source_price_basis_mismatch' for g in report.bundle.coverage_gaps),1)

    def test_rounding_difference_is_not_flagged_as_conflict(self):
        raw={"metrics":[{"metric_value":1.035,"unit":"元/Wh","evidence_locator":{"passage_id":"p1"}}]}
        report=run(raw,"10MW/40MWh，甲公司中标价为4141.622万元，合单价1.035元/Wh。")
        self.assertFalse(any(g.gap_type=='source_price_basis_mismatch' for g in report.bundle.coverage_gaps))

    def test_opt_in_preserves_existing_default_behavior(self):
        report=run(price(),"报价1.035元/Wh。",strict=False)
        self.assertEqual(report.bundle.facts[0].metrics['amount'],1.035)
        self.assertFalse(report.quality_reviews)

    def test_original_unchanged_and_repeat_validation_idempotent(self):
        raw=price()
        raw['facts'][0]['period']='2026-09-15'
        before=copy.deepcopy(raw)
        first=run(raw,"单价1.035元/Wh。")
        second=run(first.bundle.model_dump(),"单价1.035元/Wh。")
        self.assertEqual(raw,before)
        self.assertEqual(first.bundle.model_dump(),second.bundle.model_dump())
        self.assertFalse(second.quality_reviews)


if __name__ == '__main__':
    unittest.main()
