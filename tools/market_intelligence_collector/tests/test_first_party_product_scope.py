"""Pages on the target's own domain that have no article element (product / landing pages).

Synthetic DOM modelled on an observed official product page layout: a link-heavy header
with a product menu, sections of headings / cards / a short description, link cards and a
footer. The text is placeholder; it is not the captured page and does not prove live success.
"""
from mic.article_scope import FIRST_PARTY_SELECTOR
from mic.config import MICConfig
from mic.profile import TargetProfile
from mic.reader import LinkReader

OFFICIAL = "https://www.jia.example/prod.html?cid=1&id=2"
THIRD_PARTY = "https://reseller.example/show/product-2.html"
FEATURES = [
    "单模组配备超大容量高带宽显存，支持全链路ECC保护",
    "互连接口支持16卡全互连，带宽高达512GB/s",
    "符合OAM2.0规范的扣卡式模组，最大功耗700W",
]
INTRO = "甲-X200是甲公司研发的新一代通用GPU产品，配合全兼容主流生态的软件栈，适用于数据中心场景。"
MENU = "".join(f'<li><a href="/prod.html?id={i}">甲-X{i}00 训练卡</a></li>' for i in range(1, 6))
SLIDES = "".join(f'<div class="slide"><h5>{f}</h5></div>' for f in FEATURES)
PRODUCT_PAGE = f"""<html><head><title>甲-X200产品-产品与解决方案</title></head><body>
<header><ul><li><a href="/en">English</a></li><li><a href="/">中文简体</a></li></ul></header>
<div class="wrap"><div class="wrapchild">
 <div class="header"><div class="menuBox"><ul class="proList">{MENU}</ul>
   <p>甲-X100 支持64卡集群与液冷整机柜</p></div></div>
 <div class="banner"><h3>甲-X200</h3></div>
 <div class="intro"><div><p>Introduction</p><h3>介绍</h3></div><div class="intro-text">{INTRO}</div></div>
 <div class="features"><div><p>Product features</p><h3>产品特点</h3></div><div class="slides">{SLIDES}</div></div>
 <div class="scenes"><a href="/s1"><h5>大模型训练和推理场景</h5></a><a href="/s2"><h5>数据处理与分析场景</h5></a>
   <a href="/s3"><h5>通用计算与科学计算场景</h5></a></div>
</div></div></body></html>"""


def _reader(html):
    class SnapshotReader(LinkReader):
        def _fetch(self, url):
            return html, 200, "text/html"
    return SnapshotReader(MICConfig(raw={"output_schema": {"limits": {"strict_evidence_review": True}}}))


def _profile(official_domains=("jia.example",)):
    return TargetProfile.from_config({
        "target_id": "company_jia", "type": "company", "canonical_name": "甲公司股份有限公司",
        "aliases": ["甲公司"], "products": ["甲-X200"], "official_domains": list(official_domains)})


def _read(html, url, official_domains=("jia.example",)):
    return _reader(html).read("synthetic", url, _profile(official_domains))


def _body_text(result):
    return "\n".join(p.text for p in result.passages if p.passage_id != "title")


def test_official_product_page_without_article_element_is_scoped_by_content_blocks():
    result = _read(PRODUCT_PAGE, OFFICIAL)
    assert result.read_status == "read"
    assert result.body_scope["selector"] == FIRST_PARTY_SELECTOR
    text = _body_text(result)
    assert INTRO in text
    for line in FEATURES:
        assert line in text.splitlines()
    for foreign in ("甲-X100", "64卡集群", "English", "大模型训练和推理场景", "通用计算与科学计算场景"):
        assert foreign not in text
    assert len(result.passages) <= _reader(PRODUCT_PAGE).max_passages


def test_same_page_elsewhere_or_without_declared_domain_keeps_strict_failure():
    for url, domains in ((THIRD_PARTY, ("jia.example",)), (OFFICIAL, ())):
        result = _read(PRODUCT_PAGE, url, domains)
        assert result.failure_reason == "article_scope_unresolved"
        assert result.body_scope["reason"] == "article_container_not_found"
        assert result.passages == []


def test_subdomain_of_declared_official_domain_counts_as_first_party():
    result = _read(PRODUCT_PAGE, "https://developer.jia.example/doc/product.html")
    assert result.read_status == "read"
    assert result.body_scope["selector"] == FIRST_PARTY_SELECTOR


def test_official_link_directory_is_not_read_as_content():
    items = "".join(f'<li><a href="/news/{i}">甲公司发布产品更新公告第{i}期</a><span>2026-09-{10 + i}</span></li>'
                    for i in range(1, 9))
    result = _read(f"<title>新闻中心</title><body><ul>{items}</ul></body>", OFFICIAL)
    assert result.failure_reason == "article_scope_unresolved"


def test_related_label_ends_official_page_content():
    html = PRODUCT_PAGE.replace(
        '<div class="scenes">',
        '<div><h4>相关推荐</h4><div>乙-Y900是另一款产品，最大功耗900W，支持32卡互连。</div></div><div class="scenes">')
    result = _read(html, OFFICIAL)
    assert result.read_status == "read"
    assert result.body_scope["truncated_at_related_heading"] is True
    assert "乙-Y900" not in _body_text(result)
    assert FEATURES[-1] in _body_text(result)


def test_article_container_on_official_domain_is_still_preferred():
    html = ('<title>甲公司新闻</title><div><p>页面其他区域的说明文字，不属于正文内容。</p></div>'
            '<article><p>甲公司发布甲-X200通用GPU产品，面向数据中心市场。</p></article>')
    result = _read(html, OFFICIAL)
    assert result.body_scope["selector"] == "article"
    assert "页面其他区域" not in _body_text(result)


def test_challenge_page_on_official_domain_is_still_anti_bot():
    html = ("<title>访问验证</title><body><div><p>当前访问存在异常，请完成安全验证后继续访问本网站的产品与解决方案页面。</p>"
            "<p>拖动滑块完成验证，验证通过后将自动跳转到您访问的目标页面。</p></div></body>")
    assert _read(html, OFFICIAL).failure_reason == "anti_bot_page"


def test_short_first_party_body_keeps_every_line_in_order_within_limits():
    reader = _reader("")
    lines = [f"第{i}项产品特点说明文字" for i in range(12)]
    passages = reader._pack_short_body("标题", "\n".join(lines), ["参数 | 数值"])
    assert len(passages) <= reader.max_passages
    assert passages[0].passage_id == "title" and passages[1].passage_id == "t1"
    body = [line for p in passages[2:] for line in p.text.split("\n")]
    assert body == lines
    assert len({p.passage_id for p in passages}) == len(passages)


def test_first_party_body_over_the_character_budget_uses_scored_selection():
    reader = _reader("")
    assert reader._pack_short_body("标题", "说明" * (reader.max_chars // 2 + 1), []) is None
