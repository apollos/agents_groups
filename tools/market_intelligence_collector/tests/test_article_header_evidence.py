"""Synthetic DOM: an article header outside the body container reaches the model as a passage.

The fixture imitates an observed layout (headline block beside, not inside, the body
container). It is not a captured page and does not prove a live model identifies the date.
"""
from datetime import datetime, timezone

from mic.article_scope import extract_article
from mic.config import MICConfig
from mic.content_review import resolve_source_publication
from mic.content_review_policy import DATE_REVIEW_PROTOCOL, PROTOCOL
from mic.profile import TargetProfile
from mic.publication_time import PublicationWindow
from mic.reader import LinkReader
from mic.validate import BundleValidator

HEADLINE = '甲公司半年报解读：主营业务收入增长'
TIME = '2026-08-19 11:39:10'
HEADER = (f'<div class="top"><div class="crumb">首页 - 股票 - 正文</div><div class="ts"><h1>{HEADLINE}</h1>'
          f'<div class="source"><span>来源：<a href="/s">某某财经</a></span> <span id="t">{TIME}</span>'
          '</div></div></div>')
PARAGRAPHS = [f'甲公司2026年上半年营业收入为{n}亿元，同比增长{n}%，管理层表示订单充足。' for n in range(10, 22)]
BODY = '<div class="article_content">' + ''.join(f'<p>{p}</p>' for p in PARAGRAPHS) + '</div>'
RELATED = '<div class="list-box"><h3>更多文章</h3><p>乙公司公告 2026-01-02 09:00:00</p></div>'


def page(header=HEADER, title=HEADLINE + '_股票频道', tail=RELATED):
    return f'<html><head><title>{title}</title></head><body>{header}<div class="w">{BODY}{tail}</div></body></html>'


def reader(html):
    class SnapshotReader(LinkReader):
        def _fetch(self, url):
            return html, 200, 'text/html'
    return SnapshotReader(MICConfig(raw={'output_schema': {'limits': {'strict_evidence_review': True}}}))


def read(html):
    return reader(html).read('synthetic', 'https://example.invalid/a.shtml',
                             TargetProfile(target_id='t', type='company', canonical_name='甲公司'))


def test_header_outside_body_becomes_a_reserved_passage_without_a_date_label():
    result = read(page())
    assert result.read_status == 'read'
    header = [p for p in result.passages if p.passage_id == 'header']
    assert len(header) == 1 and header[0].section == '文章头部'
    assert header[0].text == f'{HEADLINE} 来源： 某某财经 {TIME}'
    assert result.passages[0].passage_id == 'title' and result.passages[1].passage_id == 'header'
    assert len(result.passages) <= 8
    # Reading never classifies the header time; only the model review can.
    assert result.publication_time['status'] == 'unknown'
    scope = result.body_scope['article_header']
    assert scope['status'] == 'found' and 'h1' in scope['h1'] and TIME not in str(scope)
    assert '首页' not in header[0].text and '2026-01-02' not in header[0].text


def test_body_and_hash_are_unchanged_by_the_header():
    with_header = read(page())
    without = read(page(header=''))
    assert with_header.content_hash == without.content_hash
    assert not any(p.passage_id == 'header' for p in without.passages)


def test_headline_not_named_by_title_or_repeated_h1_is_not_used():
    assert extract_article(page(title='别的页面'), reader('')).header == ''
    twice = page(header=HEADER + f'<h1>{HEADLINE}</h1>')
    assert extract_article(twice, reader('')).report['article_header']['reason'] == 'single_h1_missing'


def test_long_ancestor_with_other_content_is_not_swallowed():
    navigation = '<div class="menu">' + ''.join(f'<p>频道{n} 2026-02-0{n % 9 + 1} 10:00:00 推荐</p>'
                                                 for n in range(40)) + '</div>'
    html = page(header=f'<div class="wrap">{navigation}{HEADER}</div>')
    header = extract_article(html, reader('')).header
    assert header == f'{HEADLINE} 来源： 某某财经 {TIME}'


def test_header_after_body_or_inside_body_is_not_a_page_header():
    after = f'<html><head><title>{HEADLINE}</title></head><body>{BODY}{HEADER}</body></html>'
    assert extract_article(after, reader('')).report['article_header']['reason'] == 'h1_after_body'
    inside = (f'<html><head><title>{HEADLINE}</title></head><body><div class="article_content">'
              f'{HEADER}<p>{PARAGRAPHS[0]}</p></div></body></html>')
    assert extract_article(inside, reader('')).report['article_header']['reason'] == 'h1_inside_body'


def test_existing_date_review_can_bind_a_header_quote_and_pass_the_window():
    passages = read(page()).passages
    quote = f'来源： 某某财经 {TIME}'
    claim = {'id': 'publication', 'kind': 'observation', 'status': 'source_supported',
             'statement': f'本文发布于 {TIME}。', 'reason': '人工标签：标题下来源行旁的时间。',
             'evidence': [{'passage_id': 'header', 'quote': quote}], 'depends_on': [],
             'field_values': {'source_publication': {
                 'published_at': TIME, 'date_role': 'source_publication',
                 'assessment': {'protocol': DATE_REVIEW_PROTOCOL, 'possible_roles': ['source_publication'],
                                'selected_role': 'source_publication', 'basis': 'publisher_metadata',
                                'evidence': [{'passage_id': 'header', 'quote': quote}]}}}}
    raw = {'schema_version': 'bundle_extraction_v0.3', 'confidence': 0.0, 'decision': 'link_only',
           'overall_score': 60, 'content_review': {'protocol': PROTOCOL, 'claims': [claim],
                                                   'bindings': {'/source_publication': ['publication']}}}
    report = BundleValidator({}).validate(raw, passages)
    assert report.schema_valid, report.errors
    unknown = {'status': 'unknown', 'published_at': None, 'reason': 'no_explicit_publication_date'}
    resolved = resolve_source_publication([report.bundle], unknown)
    assert resolved['status'] == 'known'
    assert resolved['published_at'] == '2026-08-19T11:39:10+08:00'
    window = PublicationWindow(90, datetime(2026, 10, 31, 10, 2, 46, tzinfo=timezone.utc))
    assert window.assess(resolved)['allowed']
