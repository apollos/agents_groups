"""Synthetic DOM regressions based on an observed container relationship.

These fixtures are not the captured website DOM and do not prove live success.
"""
import unittest

from mic.article_scope import extract_article
from mic.config import MICConfig
from mic.profile import TargetProfile
from mic.reader import LinkReader


SELECTOR = '#article_cont .cc-article'
PARAGRAPHS = [
    '北极星储能网讯：2026年9月15日，河北任丘智弘100MW/400MWh新型技术路线磷酸铁锂电池+钠电池独立储能试点项目储能系统设备采购中标结果公示。',
    '项目共两个标段，一标段为磷酸铁锂电池储能系统，30MW/120MWh大容量磷酸铁锂+60MW/240MWh长寿命磷酸铁锂，远景能源中标价为19461.6万元，合单价0.518元/Wh；',
    '二标段为钠电池储能系统，10MW/40MWh钠离子储能系统，宁德时代中标价为4141.622万元，合单价1.035元/Wh。',
]
BODY = ''.join('<p>' + p + '</p>' for p in PARAGRAPHS)
FOREIGN = '<p>海辰储能4MWh钠电池系统，开启储能新阶段。</p><p>墙内开花墙外香：丰田与云储合作的其他新闻。</p>'


def snapshot(html):
    class SnapshotReader(LinkReader):
        def _fetch(self, url):
            return html, 200, 'text/html'
    return SnapshotReader(MICConfig(raw={'output_schema': {'limits': {'strict_evidence_review': True}}}))


def read(html):
    return snapshot(html).read('synthetic', 'https://example.invalid/article',
                              TargetProfile(target_id='test', type='company', canonical_name='宁德时代'))


class NestedBodyTests(unittest.TestCase):
    def test_narrow_pair_excludes_outer_news_and_overrides_broad_container(self):
        html = '<title>宁德时代中标新闻</title><div class="news-content center js-detail-center">' \
               '<div id="article_cont"><div class="cc-article">' + BODY + '</div></div>' + FOREIGN + '</div>'
        result = read(html)
        self.assertEqual(result.read_status, 'read')
        self.assertEqual(result.body_scope['selector'], SELECTOR)
        self.assertEqual([p.text for p in result.passages if p.passage_id != 'title'], PARAGRAPHS)
        self.assertEqual(extract_article(html, snapshot(html)).body.splitlines(), PARAGRAPHS)

    def test_rule_does_not_depend_on_company_or_money_keywords(self):
        text = '某研究机构发布了新的观测结果，并介绍了后续研究安排。'
        html = '<div id="article_cont"><div class="cc-article"><p>' + text + '</p></div></div>'
        self.assertEqual(extract_article(html, snapshot(html)).body, text)

    def test_outer_container_alone_is_not_a_fallback(self):
        result = read('<div id="article_cont">' + BODY + FOREIGN + '</div>')
        self.assertEqual(result.failure_reason, 'article_scope_unresolved')
        self.assertEqual(result.passages, [])

    def test_inner_class_without_parent_is_not_a_fallback(self):
        result = read('<div class="cc-article">' + BODY + '</div>')
        self.assertEqual(result.failure_reason, 'article_scope_unresolved')
        self.assertEqual(result.passages, [])

    def test_two_sibling_bodies_fail_without_using_outer_article(self):
        html = '<article><div id="article_cont"><div class="cc-article">' + BODY + \
               '</div><div class="cc-article">' + FOREIGN + '</div></div></article>'
        result = read(html)
        self.assertEqual(result.failure_reason, 'article_scope_unresolved')
        self.assertEqual(result.body_scope['reason'], 'ambiguous_article_containers')
        self.assertEqual(result.passages, [])

    def test_hidden_container_cannot_become_evidence(self):
        html = '<div hidden id="article_cont"><div class="cc-article">' + BODY + '</div></div>'
        self.assertEqual(read(html).failure_reason, 'article_scope_unresolved')

    def test_known_recommendations_inside_body_are_still_removed(self):
        html = '<div id="article_cont"><div class="cc-article">' + BODY + \
               '<div class="related-news">' + FOREIGN + '</div></div></div>'
        self.assertEqual(extract_article(html, snapshot(html)).body.splitlines(), PARAGRAPHS)


if __name__ == '__main__':
    unittest.main()
