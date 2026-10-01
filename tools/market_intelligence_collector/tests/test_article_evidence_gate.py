"""Offline scope/gate regressions. DOM fixtures are synthetic, not saved HTML."""
import copy
import json
import unittest

from mic.article_scope import extract_article
from mic.config import MICConfig
from mic.profile import TargetProfile
from mic.reader import LinkReader
from mic.schemas import Passage
from mic.validate import BundleValidator


def reader(html, strict=True):
    class SnapshotReader(LinkReader):
        def _fetch(self, url):
            return html, 200, 'text/html'
    return SnapshotReader(MICConfig(raw={'output_schema': {'limits': {'strict_evidence_review': strict}}}))


def read(html, strict=True):
    return reader(html, strict).read('synthetic-source', 'https://example.invalid/test',
                                    TargetProfile(target_id='test', type='company', canonical_name='甲公司'))


def validate(raw, text='报价1.035元/Wh。', extra=(), strict=True):
    return BundleValidator({'strict_evidence_review': strict}).validate(raw, [
        Passage(passage_id='p1', section='正文', text=text), *extra])


def metric(value=.517, unit='元/Wh', pid='p1'):
    return {'metrics': [{'metric_name': '中标单价差', 'metric_value': value, 'unit': unit,
                         'evidence_locator': {'passage_id': pid}, 'confidence': .8}]}


class ArticleScopeTests(unittest.TestCase):
    def test_article_body_excludes_other_article_and_sidebar(self):
        html = '<title>测试文章</title><div class="article-content"><p>甲公司中标40MWh储能项目。</p></div>' \
               '<aside><p>行业原料价格大幅上涨20%。</p></aside><div><p>另一新闻发生2026年9月30日。</p></div>'
        result = read(html)
        self.assertEqual(result.read_status, 'read')
        self.assertEqual([p.text for p in result.passages if p.passage_id != 'title'], ['甲公司中标40MWh储能项目。'])
        self.assertEqual(result.body_scope['status'], 'scoped')

    def test_related_block_inside_article_is_excluded(self):
        result = read('<article><p>甲公司中标40MWh储能项目。</p><div class="related-news"><p>其他公司中标100MWh。</p></div></article>')
        self.assertFalse(any('其他' in p.text for p in result.passages))

    def test_related_heading_clips_following_nodes(self):
        result = read('<article><p>甲公司中标40MWh储能项目。</p><h2>相关阅读</h2><div><p>其他公司中标100MWh。</p></div></article>')
        self.assertFalse(any('其他' in p.text for p in result.passages))
        self.assertTrue(result.body_scope['truncated_at_related_heading'])

    def test_ambiguous_sibling_articles_fail(self):
        result = read('<article><p>甲公司中标40MWh储能项目。</p></article><article><p>乙公司中标100MWh。</p></article>')
        self.assertEqual(result.failure_reason, 'article_scope_unresolved')
        self.assertEqual(result.passages, [])

    def test_nested_article_body_is_not_ambiguous(self):
        result = read('<article><h1>甲公司项目中标</h1><div itemprop="articleBody"><p>甲公司中标40MWh储能项目。</p></div></article>')
        self.assertEqual(result.read_status, 'read')

    def test_no_known_root_does_not_use_whole_page(self):
        result = read('<title>新闻</title><div><p>甲公司中标40MWh储能项目。</p></div>')
        self.assertEqual(result.failure_reason, 'article_scope_unresolved')

    def test_non_strict_legacy_read_still_works(self):
        result = read('<title>新闻</title><div><p>甲公司中标40MWh储能项目。</p></div>', strict=False)
        self.assertEqual(result.read_status, 'read')

    def test_slider_and_script_waf_still_failed(self):
        for html in ['<title>滑动验证页面</title><p>别离开，请进行验证。</p>',
                     '<script>var AC_Opt = {}; var requestInfo = {}; "CF_APP_WAF";</script>']:
            with self.subTest(html=html):
                self.assertEqual(read(html).failure_reason, 'anti_bot_page')

    def test_overlay_is_not_body_and_no_browser_action_is_needed(self):
        result = read('<article><p>甲公司中标40MWh储能项目，金额100万元。</p></article><div role="dialog">登录验证</div>')
        self.assertEqual(result.read_status, 'read')
        self.assertFalse(any('登录' in p.text for p in result.passages))

    def test_h1_and_nested_list_are_not_duplicate_evidence(self):
        result = read('<article><header><h1>甲公司中标新闻</h1></header><ul><li><p>甲公司中标40MWh储能项目。</p></li></ul></article>')
        self.assertEqual([p.text for p in result.passages], ['甲公司中标新闻', '甲公司中标40MWh储能项目。'])

    def test_plain_div_text_stays_inside_root(self):
        result = read('<article>甲公司中标40MWh储能项目。<br/>预计建设100MW新项目。</article><div>外部900万元</div>')
        self.assertEqual(result.read_status, 'read')
        self.assertFalse(any('900' in p.text for p in result.passages))

    def test_date_and_images_tables_scoped_to_article(self):
        html = '<div>2099年1月1日推荐新闻<img src="wrong.jpg"/><table><tr><td>900万元</td></tr></table></div>' \
               '<article><p>2026年9月15日甲公司中标项目。</p><img src="right.jpg"/>' \
               '<table><tr><td>金额</td><td>100万元</td></tr></table></article>'
        extracted = extract_article(html, reader(html))
        self.assertEqual(extracted.publish_time, '2026年9月15')
        self.assertEqual(extracted.image_urls, ['right.jpg'])
        self.assertEqual(extracted.tables, ['金额 | 100万元'])

    def test_empty_root_not_read_success(self):
        self.assertEqual(read('<title>标题</title><article><h1>标题</h1></article>').failure_reason, 'article_body_empty')

    def test_link_list_in_article_rejected(self):
        result = read('<article>' + ''.join('<a>甲公司项目相关采购和中标新闻</a>' for _ in range(4)) + '</article>')
        self.assertEqual(result.failure_reason, 'article_scope_unresolved')


class EvidenceGateTests(unittest.TestCase):
    def test_cross_passage_subtraction_stays_pending(self):
        report = validate(metric(), extra=[Passage(passage_id='p2', section='正文', text='合单价0.518元/Wh。')])
        self.assertEqual(report.bundle.metrics, [])
        review = next(r for r in report.quality_reviews if r['action'] == 'quarantine_metric')
        candidate = review['detail']['arithmetic_candidates'][0]
        self.assertEqual(candidate['result'], .517)
        self.assertEqual(candidate['status'], 'arithmetic_candidate_only')
        self.assertIn('审查记录=', report.bundle.coverage_gaps[-1].description)

    def test_wrong_unit_or_substring_does_not_certify(self):
        for text in ['报价11.035元/Wh。', '报价1.035元/kWh。']:
            self.assertEqual(validate(metric(1.035), text).bundle.metrics, [])

    def test_literal_zero_is_checked(self):
        self.assertEqual(validate(metric(0), '未披露单价。').bundle.metrics, [])
        self.assertEqual(len(validate(metric(0), '报价0元/Wh。').bundle.metrics), 1)

    def test_literal_and_explicit_addition_retained(self):
        self.assertEqual(len(validate(metric(1.035)).bundle.metrics), 1)
        bundle = validate(metric(360, 'MWh'), '30MW/120MWh大容量+60MW/240MWh长寿命。').bundle
        self.assertEqual(bundle.metrics[0].scope['value_evidence']['operands'], [120, 240])

    def test_title_missing_or_duplicate_citation_not_numeric_evidence(self):
        self.assertFalse(validate(metric(1.035, pid='title'), extra=[Passage(passage_id='title', section='标题', text='报价1.035元/Wh。')]).bundle.metrics)
        self.assertFalse(validate(metric(1.035, pid='missing')).bundle.metrics)
        self.assertFalse(validate(metric(1.035), extra=[Passage(passage_id='p1', section='正文', text='报价1.035元/Wh。')]).bundle.metrics)

    def test_forged_derived_certificate_does_not_promote(self):
        raw = metric()
        raw['metrics'][0]['scope'] = {'value_evidence': {'verified': True, 'value': .517}}
        self.assertFalse(validate(raw).bundle.metrics)

    def test_industry_risk_is_pending_not_company_risk(self):
        raw = {'risks': [{'risk_type': 'supplier', 'risk_summary': '原料波动（行业性表述，非甲公司特定披露）。',
                          'evidence_locator': {'passage_id': 'p1'}}]}
        report = validate(raw, '行业原料价格波动。')
        self.assertEqual(report.bundle.risks, [])

    def test_material_quality_is_research_gap_not_operating_risk(self):
        raw = {'risks': [{'risk_type': 'quality', 'risk_summary': '单一媒体报道缺少合同细节，信息可靠性有限。',
                          'evidence_locator': {'passage_id': 'p1'}}]}
        report = validate(raw)
        self.assertEqual(report.bundle.risks, [])
        self.assertEqual(report.quality_reviews[-1]['action'], 'research_quality_gap')

    def test_product_quality_risk_not_classified_as_material_quality(self):
        raw = {'risks': [{'risk_type': 'quality', 'risk_summary': '甲公司产品发生质量故障。',
                          'evidence_locator': {'passage_id': 'p1'}}]}
        self.assertEqual(len(validate(raw, '甲公司产品发生质量故障。').bundle.risks), 1)

    def test_catalyst_without_evidence_contract_pending(self):
        raw = {'catalysts': [{'description': '未来交付与投运', 'expected_date': '2027-01-01'}]}
        report = validate(raw)
        self.assertFalse(report.bundle.catalysts)
        self.assertIn('2027-01-01', report.bundle.coverage_gaps[-1].description)

    def test_gate_is_opt_in(self):
        raw = metric()
        raw['catalysts'] = [{'description': '待观察'}]
        report = validate(raw, strict=False)
        self.assertEqual(len(report.bundle.metrics), 1)
        self.assertEqual(len(report.bundle.catalysts), 1)

    def test_original_and_second_validation_unchanged(self):
        raw = metric()
        raw['catalysts'] = [{'description': '未来投运'}]
        before = copy.deepcopy(raw)
        first = validate(raw)
        second = validate(first.bundle.model_dump(mode='json'))
        self.assertEqual(raw, before)
        self.assertEqual(first.bundle.model_dump(), second.bundle.model_dump())


if __name__ == '__main__':
    unittest.main()
