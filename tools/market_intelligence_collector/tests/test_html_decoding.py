"""HTTP HTML is decoded from response bytes: BOM, HTTP charset, then HTML meta.

Offline doubles only; no page is fetched.
"""
import json

import httpx

from mic import reader as reader_mod
from mic.browser.contracts import FetchResult
from mic.browser.fetch import HTML_DECODE_FAILED, HttpFetcher, decode_html_bytes
from mic.reader import LinkReader

ARTICLE = "<p>证券之星消息，兆易创新2026年上半年营业收入115.66亿元，同比增长178.67%。</p>"


def _page(meta: str = "") -> str:
    return f"<html><head>{meta}<title>兆易创新半年报</title></head><body>{ARTICLE}</body></html>"


GBK_META = _page('<meta http-equiv="Content-Type" content="text/html; charset=gbk">').encode("gbk")


def _response(content: bytes, content_type: str, url="https://news.example.invalid/a.html"):
    return httpx.Response(200, content=content, headers={"content-type": content_type},
                          request=httpx.Request("GET", url))


def test_gbk_meta_without_http_charset_decodes_from_bytes():
    decoded = decode_html_bytes(GBK_META, "text/html")
    assert decoded.error is None
    assert (decoded.encoding, decoded.encoding_source) == ("gbk", "html_meta")
    assert "营业收入115.66亿元" in decoded.html
    # The previous text path decoded the same bytes as UTF-8 and produced mojibake.
    assert "营业收入" not in _response(GBK_META, "text/html").text


def test_http_header_charset_precedes_meta_and_bom_precedes_both():
    utf8_with_gbk_meta = _page('<meta charset="gbk">').encode("utf-8")
    decoded = decode_html_bytes(utf8_with_gbk_meta, "text/html; charset=UTF-8")
    assert (decoded.encoding, decoded.encoding_source) == ("utf-8", "http_header")
    assert "营业收入" in decoded.html
    bom = decode_html_bytes(b"\xef\xbb\xbf" + _page().encode("utf-8"), "text/html; charset=gbk")
    assert bom.encoding_source == "bom" and "营业收入" in bom.html


def test_undeclared_utf8_is_unchanged():
    decoded = decode_html_bytes(_page().encode("utf-8"), "text/html")
    assert (decoded.encoding, decoded.encoding_source) == ("utf-8", "undeclared_utf8")
    assert decoded.html == _page()


def test_undeclared_non_utf8_bytes_are_an_explicit_error_not_a_guess():
    decoded = decode_html_bytes(_page().encode("gbk"), "text/html")
    assert decoded.html is None and decoded.error == HTML_DECODE_FAILED
    assert decoded.tried == [{"label": "utf-8", "source": "undeclared_utf8", "result": "decode_error"}]


def test_http_fetcher_records_encoding_and_keeps_pdf_bytes():
    fetcher = HttpFetcher(getter=lambda url, **_: _response(GBK_META, "text/html"))
    fetched = fetcher.fetch("https://news.example.invalid/a.html")
    assert fetched.ok and "营业收入115.66亿元" in fetched.html
    assert fetched.diagnostics()["encoding"] == "gbk"
    assert fetched.diagnostics()["encoding_source"] == "html_meta"

    pdf = b"%PDF-1.4\n\xd6\xd0\xce\xc4 bytes"
    fetcher = HttpFetcher(getter=lambda url, **_: _response(pdf, "application/pdf", "https://x.invalid/a.pdf"))
    fetched = fetcher.fetch("https://x.invalid/a.pdf")
    assert fetched.content == pdf and fetched.html is None and fetched.encoding is None


def test_reader_http_path_decodes_bytes_and_reports_decode_failure(monkeypatch, config, tmp_path):
    reader = LinkReader(config, search_provider=None)
    monkeypatch.setattr(reader_mod.httpx, "get", lambda url, **_: _response(GBK_META, "text/html"))
    fetched = reader._fetch_http_result("https://news.example.invalid/a.html")
    assert "营业收入115.66亿元" in fetched.html
    assert (fetched.encoding, fetched.encoding_source) == ("gbk", "html_meta")

    monkeypatch.setenv("MIC_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("MIC_FETCH_DEBUG", raising=False)
    monkeypatch.setattr(reader_mod.httpx, "get", lambda url, **_: _response(_page().encode("gbk"), "text/html"))
    fetched = reader._fetch_http_result("https://news.example.invalid/b.html")
    assert fetched.blocked_reason == HTML_DECODE_FAILED and fetched.html is None
    assert not (tmp_path / "fetch_decode_failures").exists()
    result = reader._parse_fetch("link_b", fetched, None)
    assert result.read_status == "failed" and result.failure_reason == HTML_DECODE_FAILED

    monkeypatch.setenv("MIC_FETCH_DEBUG", "1")
    reader._fetch_http_result("https://news.example.invalid/b.html")
    (meta,) = (tmp_path / "fetch_decode_failures").glob("*.json")
    assert json.loads(meta.read_text())["url"] == "https://news.example.invalid/b.html"
    assert meta.with_suffix(".bin").read_bytes() == _page().encode("gbk")


def test_reader_pdf_and_browser_dom_paths_are_not_redecoded(monkeypatch, config):
    reader = LinkReader(config, search_provider=None)
    pdf = b"%PDF-1.4\n\xd6\xd0 bytes"
    monkeypatch.setattr(reader_mod.httpx, "get",
                        lambda url, **_: _response(pdf, "application/pdf", "https://x.invalid/a.pdf"))
    fetched = reader._fetch_http_result("https://x.invalid/a.pdf")
    assert fetched.content == pdf and fetched.encoding is None

    dom = FetchResult(transport="browser", requested_url="https://x.invalid/c", final_url="https://x.invalid/c",
                      http_status=200, content_type="text/html", html=_page())
    decode_calls, parsed = [], []
    monkeypatch.setattr("mic.browser.fetch.decode_html_bytes", lambda *a: decode_calls.append(a))

    def parse_raw(link, raw, *args, **kwargs):
        parsed.append(raw)
        return reader_mod.ReadResult(source_link_id=link, read_status="read", passages=[])

    monkeypatch.setattr(reader, "_parse_raw", parse_raw)
    reader._parse_fetch("link_c", dom, None)
    assert decode_calls == [] and parsed == [dom.html] and parsed[0] is dom.html
