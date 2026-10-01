"""Synthetic recommendation-boundary cases; not a saved website DOM."""
import unittest

from mic.article_scope import extract_article
from mic.config import MICConfig
from mic.profile import TargetProfile
from mic.reader import LinkReader

BODY = [
    '北极星储能网讯：2026年9月15日，河北任丘智弘100MW/400MWh独立储能试点项目中标结果公示。',
    '一标段为磷酸铁锂储能系统，远景能源中标价为19461.6万元，合单价0.518元/Wh；',
    '二标段为10MW/40MWh钠离子储能系统，宁德时代中标价为4141.622万元，合单价1.035元/Wh。',
]
RECOMMENDATION = '0.52~1.06元/Wh！远景能源/宁德时代等入围河北400MWh储能系统招标！'


def html_page(extra, body=BODY):
    return '<title>宁德时代中标新闻</title><div id="article_cont"><div class="cc-article">' + \
           ''.join('<p>' + p + '</p>' for p in body) + extra + '</div></div>'


def extract(html):
    class SnapshotReader(LinkReader):
        def _fetch(self, url):
            return html, 200, 'text/html'
    reader = SnapshotReader(MICConfig(raw={'output_schema': {'limits': {'strict_evidence_review': True}}}))
    result = reader.read('test', 'https://example.invalid/news',
                         TargetProfile(target_id='test', type='company', canonical_name='宁德时代'))
    return result, extract_article(html, reader)


class RelatedBoundaryTests(unittest.TestCase):
    def assert_article_only(self, html):
        result, article = extract(html)
        self.assertEqual(result.read_status, 'read')
        self.assertEqual(article.body.splitlines(), BODY)
        self.assertEqual([p.text for p in result.passages if p.passage_id != 'title'], BODY)
        self.assertTrue(result.body_scope['truncated_at_related_heading'])
        return result, article

    def test_inline_related_link_is_not_article_evidence(self):
        self.assert_article_only(html_page('<p>相关阅读： <a href="/older.shtml">' + RECOMMENDATION + '</a></p>'))

    def test_ascii_colon_and_spacing_are_recognized(self):
        for label in ('相关阅读: ', '相关阅读 ： ', '相关新闻：', '相关推荐 : '):
            with self.subTest(label=label):
                self.assert_article_only(html_page('<p>' + label + RECOMMENDATION + '</p>'))

    def test_label_split_into_inline_tags_still_marks_boundary(self):
        self.assert_article_only(html_page('<p><span>相关阅读</span><span>：</span><a>' + RECOMMENDATION + '</a></p>'))

    def test_link_can_be_plain_text_without_a_tag(self):
        self.assert_article_only(html_page('<p>相关阅读：' + RECOMMENDATION + '</p>'))

    def test_media_after_related_boundary_is_not_exported(self):
        result, article = self.assert_article_only(html_page('<p>相关阅读：' + RECOMMENDATION + '</p>'
                    '<div><p>后续推荐新闻及其数值900万元。</p><img src="unrelated.jpg">'
                    '<table><tr><td>不应导出的报价</td></tr></table></div>'))
        self.assertEqual(article.image_urls, [])
        self.assertEqual(article.tables, [])

    def test_mid_sentence_mention_is_retained(self):
        text = '公告说明相关阅读：技术说明文件另附，项目交付安排保持不变。'
        result, article = extract(html_page('<p>' + text + '</p>'))
        self.assertIn(text, article.body)
        self.assertNotIn('truncated_at_related_heading', result.body_scope)

    def test_quoted_label_is_not_mistaken_for_a_heading(self):
        text = '“相关阅读：”是页面区块的标签，该公告仍在介绍本次项目。'
        result, article = extract(html_page('<p>' + text + '</p>'))
        self.assertIn(text, article.body)
        self.assertNotIn('truncated_at_related_heading', result.body_scope)

    def test_label_without_separator_inside_prose_is_retained(self):
        text = '相关阅读材料介绍了设备验收方法以及后续维护要求。'
        result, article = extract(html_page('<p>' + text + '</p>'))
        self.assertIn(text, article.body)
        self.assertNotIn('truncated_at_related_heading', result.body_scope)


if __name__ == '__main__':
    unittest.main()
