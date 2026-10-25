"""Synthetic DOM contracts; these fixtures do not prove a live index parses."""
import json
import pytest

from mic.browser.index_scope import extract_index_scope, MAX_ENTRIES, MAX_HTML_CHARS, MAX_DOM_DEPTH
from mic.browser.index_scope import MAX_LINK_URL_CHARS, MAX_TOTAL_MATERIAL_CHARS, MAX_PAGINATION_CHARS


def rows(n=2):
    return "".join(
        f'<li><a href="/files/{i}.pdf">条目 {i} 的附件标题</a>'
        f'<time datetime="2026-10-{i % 28 + 1:02d}">2026-10-{i % 28 + 1:02d}</time></li>'
        for i in range(n)
    )


def extract(html):
    return extract_index_scope(html, base_url="https://example.invalid/path/list")


def test_list_without_main_or_article_preserves_entry_date_link_and_original_locator():
    html = '<title>公开材料目录</title><nav><ul>' + rows() + '</ul></nav><div><ul>' + rows() + '</ul></div>'
    result = extract(html)
    assert result["status"] == "scoped"
    assert result["title"] == "公开材料目录"
    assert len(result["entries"]) == 2
    first = result["entries"][0]
    assert first["text"] == "条目 0 的附件标题 2026-10-01"
    assert first["links"] == [{"text": "条目 0 的附件标题", "url": "https://example.invalid/files/0.pdf"}]
    assert first["date_hints"][0]["datetime"] == "2026-10-01"
    assert "div:nth-of-type(1) > ul:nth-of-type(1) > li:nth-of-type(1)" in first["locator"]
    assert result["publication_time"]["published_at"] is None
    assert result["publication_time"]["status"] == "unknown"
    assert result["scope"]["coverage"] == "visible_entries_only"


def test_table_keeps_row_dates_and_headers_separate():
    result = extract('<table><tr><th>条目标题</th><th>时间</th></tr>'
                     '<tr><td><a href="/one">材料一</a></td><td>2026-09-01</td></tr>'
                     '<tr><td><a href="/two">材料二</a></td><td>2026-10-01</td></tr></table>')
    assert result["status"] == "scoped"
    assert [row["text"] for row in result["entries"]] == ["材料一 2026-09-01", "材料二 2026-10-01"]
    assert result["scope"]["containers"][0]["table_headers"] == ["条目标题", "时间"]
    assert result["publication_time"]["published_at"] is None


def test_repeated_divs_preserve_date_with_its_own_entry():
    result = extract('<div><div><a href="/a">Alpha note</a><span>2026/10/01</span></div>'
                     '<div><a href="/b">Beta note</a><span>2026/09/01</span></div></div>')
    assert result["status"] == "scoped"
    assert [r["text"] for r in result["entries"]] == ["Alpha note 2026/10/01", "Beta note 2026/09/01"]


@pytest.mark.parametrize("wrapper", [
    '<nav>{}</nav>', '<footer>{}</footer>', '<aside>{}</aside>',
    '<div role="navigation">{}</div>', '<div hidden>{}</div>',
    '<div aria-hidden="true">{}</div>', '<div style="display:none">{}</div>',
    '<div style="visibility: hidden !important">{}</div>', '<div class="related-news">{}</div>',
    '<div class="recommended">{}</div>', '<div class="main-menu">{}</div>',
])
def test_chrome_hidden_and_recommendations_do_not_become_evidence(wrapper):
    html = wrapper.format('<ul>' + rows() + '</ul>') + '<ul>' + rows() + '</ul>'
    result = extract(html)
    assert result["status"] == "scoped"
    assert len(result["entries"]) == 2
    assert result["scope"]["candidate_containers"] == 1


def test_plain_unmarked_menu_is_not_a_content_index():
    result = extract('<ul><li><a href="/products">Product overview</a></li>'
                     '<li><a href="/contact">Contact information</a></li></ul>')
    assert result["status"] == "unresolved"
    assert result["entries"] == []


def test_layout_table_links_are_not_an_index_without_entry_metadata():
    result = extract('<table><tr><td><a href="/products">Product overview</a></td><td>Browse choices</td></tr>'
                     '<tr><td><a href="/contact">Contact information</a></td><td>Reach our team</td></tr></table>')
    assert result["status"] == "unresolved"
    assert result["entries"] == []


def test_two_independent_lists_are_explicitly_ambiguous():
    result = extract('<section><ul>' + rows() + '</ul></section><section><ul>' + rows() + '</ul></section>')
    assert result["status"] == "unresolved"
    assert result["reason"] == "ambiguous_index_containers"
    assert result["entries"] == []


def test_pagination_is_recorded_without_fetching_or_claiming_completeness():
    result = extract('<ul>' + rows() + '</ul><nav class="pagination">'
                     '<a aria-current="page" href="?page=1">1</a><a rel="next" href="?page=2">2</a></nav>')
    assert result["status"] == "scoped"
    pagination = result["scope"]["pagination"]
    assert pagination["detected"] is True
    assert pagination["current"] == ["1"]
    assert pagination["links"][1] == {"text": "2", "url": "https://example.invalid/path/list?page=2", "rel": ["next"]}
    assert result["scope"]["followed_links"] == 0
    assert result["scope"]["coverage"] == "visible_entries_only"


def test_rel_navigation_is_pagination_even_without_css_class():
    result = extract('<ul>' + rows() + '</ul><nav><a rel="next" href="?p=2">Next</a></nav>')
    assert result["scope"]["pagination"]["detected"] is True
    assert result["scope"]["pagination"]["links"][0]["url"] == "https://example.invalid/path/list?p=2"


def test_hidden_entries_are_removed_before_scope_and_locator_stays_original():
    result = extract('<ul><li hidden><a href="/bad.pdf">隐藏材料</a><time>2030-01-01</time></li>' + rows() + '</ul>')
    assert result["status"] == "scoped"
    assert "li:nth-of-type(2)" in result["entries"][0]["locator"]
    assert "2030-01-01" not in str(result["entries"])


def test_entry_limit_reports_omissions_and_keeps_whole_entries():
    result = extract('<ul>' + rows(MAX_ENTRIES + 2) + '</ul>')
    assert len(result["entries"]) == MAX_ENTRIES
    assert result["scope"]["eligible_entries"] == MAX_ENTRIES + 2
    assert result["scope"]["truncated"] is True
    assert result["scope"]["truncation_reasons"] == ["entry_count"]


def test_oversized_entry_is_omitted_without_cutting_evidence_or_pairing_neighbor_date():
    huge = '<li><a href="/huge.pdf">' + '很长的条目' * 500 + '</a><time>2030-01-01</time></li>'
    result = extract('<ul>' + huge + rows() + '</ul>')
    assert result["status"] == "scoped"
    assert len(result["entries"]) == 2
    assert result["entries"][0]["entry_id"] == "index_entry_2"
    assert result["scope"]["truncation_reasons"] == ["entry_chars"]
    assert "2030-01-01" not in str(result["entries"])


def test_oversized_html_is_rejected_before_parsing():
    result = extract('x' * (MAX_HTML_CHARS + 1))
    assert result["status"] == "unresolved"
    assert result["reason"] == "index_html_limit_exceeded"
    assert result["scope"]["truncated"] is True


def test_deep_dom_is_rejected_before_locator_allocation_grows_unbounded():
    result = extract('<div>' * (MAX_DOM_DEPTH + 1) + '<ul>' + rows() + '</ul>' + '</div>' * (MAX_DOM_DEPTH + 1))
    assert result["status"] == "unresolved"
    assert result["reason"] == "index_dom_depth_exceeded"


def test_script_links_and_javascript_only_entries_never_count():
    result = extract('<script>"<ul>' + rows() + '</ul>"</script><ul>'
                     '<li><a href="javascript:run()">调用函数</a><time>2026-10-01</time></li>'
                     '<li><a href="#">页内切换</a><time>2026-10-02</time></li></ul>')
    assert result["status"] == "unresolved"
    assert result["entries"] == []


def test_malformed_url_is_ignored_without_disrupting_other_entries():
    result = extract('<ul><li><a href="https://[broken">损坏链接</a><time>2030-01-01</time></li>' + rows() + '</ul>')
    assert result["status"] == "scoped"
    assert len(result["entries"]) == 2


def test_oversized_url_omits_entire_entry_without_slicing_or_wrong_date_pairing():
    href = "/" + "x" * MAX_LINK_URL_CHARS + ".pdf"
    result = extract('<ul><li><a href="' + href + '">短标题附件</a>'
                     '<time>2030-01-01</time></li>' + rows() + '</ul>')
    assert result["status"] == "scoped" and len(result["entries"]) == 2
    assert result["scope"]["truncated"] is True
    assert "link_url_chars" in result["scope"]["truncation_reasons"]
    assert "2030-01-01" not in json.dumps(result["entries"], ensure_ascii=False)
    assert result["entries"][0]["links"][0]["url"] == "https://example.invalid/files/0.pdf"


def test_large_date_attribute_omits_whole_entry_under_metadata_limit():
    row = '<li><a href="/huge.pdf">短标题附件</a><time datetime="' + 'x' * 9000 + '">2030-01-01</time></li>'
    result = extract('<ul>' + row + rows() + '</ul>')
    assert result["status"] == "scoped" and len(result["entries"]) == 2
    assert result["scope"]["truncation_reasons"] == ["entry_metadata_chars"]
    assert "2030-01-01" not in json.dumps(result["entries"], ensure_ascii=False)


def test_short_titles_with_many_bounded_links_still_obey_total_material_budget():
    entries = ''.join('<li><a href="/' + 'x' * 1500 + f'/{i}.pdf">短标题附件 {i}</a>'
                      '<time>2026-10-01</time></li>' for i in range(MAX_ENTRIES))
    result = extract('<ul>' + entries + '</ul>')
    assert result["status"] == "scoped" and 1 < len(result["entries"]) < MAX_ENTRIES
    assert "total_material_chars" in result["scope"]["truncation_reasons"]
    assert len(json.dumps(result, ensure_ascii=False)) <= MAX_TOTAL_MATERIAL_CHARS
    assert all(len(e["links"][0]["url"]) > 1500 for e in result["entries"])


def test_pagination_url_and_label_metadata_are_bounded_and_omissions_reported():
    pager = ('<nav class="pagination"><a rel="next" href="/' + 'x' * 3000 + '">Next</a>'
             '<span aria-current="page">' + '9' * 5000 + '</span>'
             '<a href="?p=2">2</a></nav>')
    result = extract('<ul>' + rows() + '</ul>' + pager)
    assert result["status"] == "scoped"
    assert {"link_url_chars", "pagination_metadata_chars"}.issubset(result["scope"]["truncation_reasons"])
    pagination = result["scope"]["pagination"]
    assert pagination["detected"] is True and pagination["current"] == []
    assert pagination["links"][0]["url"].endswith("?p=2")
    assert len(json.dumps(pagination, ensure_ascii=False)) <= MAX_PAGINATION_CHARS


def test_oversized_table_header_cannot_escape_through_scope_metadata():
    result = extract('<table><tr><th>' + '标题' * 5000 + '</th></tr>'
                     '<tr><td><a href="/a.pdf">材料附件一</a></td><td>2026-10-01</td></tr>'
                     '<tr><td><a href="/b.pdf">材料附件二</a></td><td>2026-09-01</td></tr></table>')
    assert result["status"] == "unresolved"
    assert result["reason"] == "index_scope_metadata_limit_exceeded"
    assert result["entries"] == [] and result["scope"]["containers"] == []
