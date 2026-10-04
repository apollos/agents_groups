"""Synthetic fixtures model the bulletin structure fetched on 2026-10-04.

No network or model calls. Host/id binding is tested separately from body
extraction and publication-window eligibility.
"""
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from mic.article_scope import extract_article
from mic.browser.contracts import RawResult
from mic.browser.relevance import content_form_ok, judge
from mic.config import MICConfig
from mic.pipeline import Pipeline, RunStats
from mic.publication_time import PublicationWindow, extract_publication
from mic.reader import LinkReader
from mic.run_context import TargetIdentity
from mic.schemas import SearchHit, TriageResult

URL = "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=11913341"
TABLE = '''<table id="allbulletin"><thead><tr><th>测试公司：关于签署合同的公告
<font>（<a href="https://file.finance.sina.com.cn/sample/11913341.PDF">下载公告</a>）</font>
</th></tr></thead><tbody><tr><td class="graybgH2">公告日期:2026-01-14</td></tr>
<tr><td><div id="box" class="graybgH2"><div id="content">
<p>测试公司签署设备供应合同，金额100万元。</p><p>2026年9月30日是约定交付日期。</p>
<table><tr><td>测试项目</td><td>100万元</td></tr></table>
<div class="related"><p>另一公司的项目金额900万元。</p></div>
</div></div>附件：<a href="/other.pdf">公告原文</a></td></tr></tbody></table>'''
HTML = '<html><head><title>新浪财经公司公告</title></head><body><div>外部股票行情888万元。</div>' + TABLE + '</body></html>'


def extract(html=HTML, url=URL):
    reader = LinkReader(MICConfig(raw={"output_schema": {"limits": {"strict_evidence_review": True}}}))
    return extract_article(html, reader, url)


def publication(html=HTML, url=URL):
    return extract_publication(BeautifulSoup(html, "lxml"), url)


def test_bound_sina_body_and_labelled_publication():
    value = extract()
    assert value.report["status"] == "scoped"
    assert value.report["selector"] == "sina:allbulletin#content"
    assert value.title == "测试公司：关于签署合同的公告"
    assert "金额100万元" in value.body and "约定交付日期" in value.body
    assert "888万元" not in value.body and "900万元" not in value.body
    assert "附件" not in value.body and "公告日期" not in value.body
    assert value.tables == ["测试项目 | 100万元"]
    assert value.publish_time == "2026-01-14T00:00:00+08:00"
    date = publication()
    assert date["source"] == "sina:allbulletin.announcement-date"
    window = PublicationWindow(30, datetime(2026, 10, 4, tzinfo=timezone.utc))
    assert window.assess(date)["status"] == "outside_time_window"
    assert window.assess(publication(HTML.replace("公告日期:2026-01-14", "公告日期:2026-09-15")))["allowed"]
    alias = URL.replace("vip.stock", "money") + "&stockid=688005"
    assert publication(url=alias)["status"] == "known"


def test_sina_host_document_id_and_visibility_binding():
    cases = [
        (HTML, URL.replace("vip.stock.finance.sina.com.cn", "vip.stock.finance.sina.com.cn.example.net")),
        (HTML, URL.replace("11913341", "999")),
        (HTML, URL + "&id=999"),
        (HTML, URL.replace("vCB_AllBulletinDetail.php", "vCB_AllBulletin.php")),
        (HTML.replace("file.finance.sina.com.cn", "unrelated.example"), URL),
        (HTML.replace("<table id=\"allbulletin\">", '<table id="allbulletin" hidden>'), URL),
        (HTML.replace('<div id="content">', '<div id="content" style="display: none">'), URL),
        (HTML.replace(TABLE, '<aside>' + TABLE + '</aside>'), URL),
        (HTML.replace(TABLE, TABLE + TABLE), URL),
    ]
    for html, url in cases:
        assert publication(html, url)["status"] == "unknown", url
        assert extract(html, url).report["status"] == "unresolved", url


def test_body_date_url_date_and_conflict_are_not_recent_evidence():
    no_date = HTML.replace("公告日期:2026-01-14", "业务日期:2026-01-14")
    assert extract(no_date).report["status"] == "scoped"
    assert publication(no_date)["status"] == "unknown"
    assert publication(no_date.replace('/sample/', '/2026-09-30/'))["status"] == "unknown"
    hidden = HTML.replace('class="graybgH2">公告日期', 'class="graybgH2" hidden>公告日期')
    assert publication(hidden)["status"] == "unknown"
    conflict = HTML.replace('<title>', '<meta property="article:published_time" content="2026-09-30"><title>')
    assert publication(conflict)["status"] == "conflict"


def test_directory_forms_and_stock_code_are_not_articles():
    cases = [
        ("https://data.eastmoney.com/zdht/detail/300750.html", "宁德时代重大合同 _ 数据中心 _ 东方财富网"),
        ("https://data.eastmoney.com/notices/stock/300750.html", "宁德时代 （300750） 公告列表 _ 数据中心"),
        ("https://www.bidcenter.com.cn/enterprise/ndsdxnykjyxgs/1", "宁德时代最新招标采购公告"),
        ("https://www.szse.cn/disclosure/listed/notice/index.html?stock=300750", "上市公司公告"),
        ("https://other.example/stock", "宁德时代 （300750） 公告列表"),
    ]
    identity = TargetIdentity(target_id="company_300750", canonical_name="宁德时代")
    for url, title in cases:
        assert content_form_ok(url, title)[0] is False
        rel = judge(RawResult(title=title, raw_href=url, url=url, snippet="宁德时代 中标", rank_in_page=1), "宁德时代 中标", identity)
        hit = SearchHit(query="宁德时代 中标", title=title, url=url, discovery={"relevance": rel.as_dict()})
        promoted = TriageResult(source_link_id="fixture", triage_decision="read", read_priority=99, need_model=True)
        final = Pipeline._apply_read_gate(hit, promoted, RunStats())
        assert final.triage_decision == "link_record_only" and not final.need_model


def test_individual_articles_and_lookalike_hosts_remain_eligible():
    for url in [
        "https://data.eastmoney.com/notices/detail/300750/AN20261004000001.html",
        "https://finance.eastmoney.com/a/20261004000001.html",
        "https://data.eastmoney.com.example.net/zdht/detail/300750.html",
        "https://news.example/zdht/detail/300750.html",
        "https://www.bidcenter.com.cn/news-123456-4.html",
        "https://www.bidcenter.com.cn/enterprise/catl/project.html",
        URL,
    ]:
        assert content_form_ok(url, "宁德时代项目中标公告")[0]
    # A date in a news headline remains allowed by the existing year exception.
    assert content_form_ok("https://example.org/news/123", "2026年公司专题研究发布")[0]


def test_absence_claim_is_limited_to_available_material_without_score_changes():
    from copy import deepcopy
    from mic.schemas import Passage
    from mic.validate import BundleValidator
    raw = {"decision": "save_structured", "overall_score": 66,
           "brief": {"uncertainty": "单一媒体来源，无官方中标文件；订单规模小，对宁德时代收入影响有限。"}}
    original = deepcopy(raw)
    validator = BundleValidator({"strict_evidence_review": True})
    passages = [Passage(passage_id="p0", section="正文", text="宁德时代中标设备采购项目，金额100万元。")]
    result = validator.validate(raw, passages)
    assert result.schema_valid and result.bundle.overall_score == 66
    assert "单一媒体来源" in result.bundle.brief.uncertainty
    assert "无官方中标文件" not in result.bundle.brief.uncertainty
    assert "当前提供材料" in result.bundle.brief.uncertainty
    assert "是否另有披露尚未核查" in result.bundle.brief.uncertainty
    assert "影响有限" not in result.bundle.brief.uncertainty
    assert raw == original
    replay = validator.validate(result.bundle.model_dump(), passages)
    assert replay.bundle.model_dump() == result.bundle.model_dump()
