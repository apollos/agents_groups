"""Tests for the vision rescue path: scanned PDFs and embedded page images are
transcribed via OpenClaw's /v1/responses API (input_image + x-openclaw-model)."""

import io
from types import SimpleNamespace

import mic.modeling.vision as vision_mod
from mic.modeling.vision import VisionExtractor
from mic.profile import TargetProfile
from mic.reader import LinkReader

_DEFAULT_TEXT = "宁德时代与现代汽车签订5亿元动力电池订单"


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _responses_payload(text=_DEFAULT_TEXT):
    return {"status": "completed", "output": [{
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": text}]}]}


def _vision(config, **overrides) -> VisionExtractor:
    v = VisionExtractor(config)
    v.adapter = SimpleNamespace(endpoint="http://127.0.0.1:18789/v1",
                                api_key="stub-token")
    for key, val in overrides.items():
        setattr(v, key, val)
    return v


def _patch_post(monkeypatch, captured: dict, text=_DEFAULT_TEXT):
    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, body=json, headers=headers)
        return _FakeResp(_responses_payload(text))
    monkeypatch.setattr(vision_mod.httpx, "post", fake_post)


def test_vision_speaks_responses_api(config, monkeypatch):
    captured: dict = {}
    _patch_post(monkeypatch, captured)
    v = _vision(config, model_override="vendor/vision-model-x")
    text = v.transcribe_pdf_pages([b"fake-jpeg-1", b"fake-jpeg-2"])
    assert text and "宁德时代" in text
    assert v.calls_used == 1
    assert captured["url"] == "http://127.0.0.1:18789/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer stub-token"
    # The override header forces a vision-capable backend on the gateway side.
    assert captured["headers"]["x-openclaw-model"] == "vendor/vision-model-x"
    body = captured["body"]
    assert body["model"] == v.request_model
    content = body["input"][0]["content"]
    assert content[0]["type"] == "input_text"
    images = [c for c in content if c["type"] == "input_image"]
    assert len(images) == 2
    assert images[0]["source"]["type"] == "base64"
    assert images[0]["source"]["media_type"] == "image/jpeg"


def test_vision_model_override_env_wins_over_yaml(config, monkeypatch):
    monkeypatch.setenv("OPENCLAW_VISION_MODEL", "vendor/from-env")
    v = VisionExtractor(config)
    assert v.model_override == "vendor/from-env"


def test_vision_no_override_header_when_unset(config, monkeypatch):
    captured: dict = {}
    _patch_post(monkeypatch, captured)
    v = _vision(config, model_override="")
    assert v.transcribe_pdf_pages([b"img"])
    assert "x-openclaw-model" not in captured["headers"]


def test_vision_unavailable_without_key_or_budget(config, monkeypatch):
    captured: dict = {}
    _patch_post(monkeypatch, captured)
    v = _vision(config)
    v.adapter.api_key = ""
    assert not v.available
    assert v.transcribe_pdf_pages([b"img"]) is None

    v2 = _vision(config, max_calls_per_run=1)
    assert v2.transcribe_pdf_pages([b"img"])
    assert not v2.available  # budget exhausted
    assert v2.transcribe_pdf_pages([b"img"]) is None


def test_vision_filters_no_information_images(config, monkeypatch):
    _patch_post(monkeypatch, {}, text="无有效信息")
    v = _vision(config)
    assert v.transcribe_page_images([b"img"]) is None


def test_vision_handles_gateway_error(config, monkeypatch):
    import httpx as _httpx

    def fail_post(url, json=None, headers=None, timeout=None):
        raise _httpx.ConnectError("gateway down")
    monkeypatch.setattr(vision_mod.httpx, "post", fail_post)
    v = _vision(config)
    assert v.transcribe_pdf_pages([b"img"]) is None
    assert v.calls_used == 1  # the attempt still consumed budget


def _scanned_pdf() -> bytes:
    """A valid PDF with no text layer (like a scanned announcement)."""
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_reader_rescues_scanned_pdf_via_vision(config, monkeypatch):
    _patch_post(monkeypatch, {})
    vision = _vision(config)
    reader = LinkReader(config, search_provider=None, vision=vision)
    profile = TargetProfile.from_config(config.get_target_profile("company_300750"))
    monkeypatch.setattr(
        reader, "_fetch", lambda url: (_scanned_pdf(), 200, "application/pdf"))
    monkeypatch.setattr("mic.reader.render_pdf_pages",
                        lambda data, max_pages, scale=2.0: [b"page1-jpeg"])
    res = reader.read("l1", "https://static.example.com/scan.pdf", profile)
    assert res.read_status == "read"
    assert res.document_type == "pdf"
    assert any("宁德时代" in p.text for p in res.passages)
    assert vision.calls_used == 1


def test_reader_scanned_pdf_still_fails_without_vision(config, monkeypatch):
    reader = LinkReader(config, search_provider=None, vision=None)
    profile = TargetProfile.from_config(config.get_target_profile("company_300750"))
    monkeypatch.setattr(
        reader, "_fetch", lambda url: (_scanned_pdf(), 200, "application/pdf"))
    res = reader.read("l1", "https://static.example.com/scan.pdf", profile)
    assert res.read_status == "failed"
    assert res.failure_reason == "pdf_extract_failed"


_THIN_IMAGE_HTML = """
<html><head><title>宁德时代签订重大合同</title></head><body>
<p>详情见下方公告截图。</p>
<img src="https://img.example.com/announce.jpg" width="800" height="1200"/>
<img src="https://img.example.com/logo.png" width="32" height="32"/>
</body></html>
"""


def test_reader_rescues_thin_html_page_with_images(config, monkeypatch):
    _patch_post(monkeypatch, {})
    vision = _vision(config, html_images_enabled=True)
    reader = LinkReader(config, search_provider=None, vision=vision)
    profile = TargetProfile.from_config(config.get_target_profile("company_300750"))
    monkeypatch.setattr(
        reader, "_fetch", lambda url: (_THIN_IMAGE_HTML, 200, "text/html"))
    monkeypatch.setattr(
        reader, "_fetch_images", lambda urls, page_url: [b"announce-jpeg"])
    res = reader.read("l1", "https://news.example.com/a.html", profile)
    assert res.read_status == "read"
    img_passages = [p for p in res.passages if p.section.startswith("图片转写")]
    assert len(img_passages) == 1
    assert "宁德时代" in img_passages[0].text


def test_reader_skips_image_rescue_when_disabled_or_body_rich(config, monkeypatch):
    _patch_post(monkeypatch, {})  # safety net: must not be hit anyway
    # Disabled by default: thin page does not trigger a vision call.
    vision = _vision(config, html_images_enabled=False)
    reader = LinkReader(config, search_provider=None, vision=vision)
    profile = TargetProfile.from_config(config.get_target_profile("company_300750"))
    monkeypatch.setattr(
        reader, "_fetch", lambda url: (_THIN_IMAGE_HTML, 200, "text/html"))
    reader.read("l1", "https://news.example.com/a.html", profile)
    assert vision.calls_used == 0

    # Enabled but the body is rich: no vision call either.
    rich_html = _THIN_IMAGE_HTML.replace(
        "<p>详情见下方公告截图。</p>",
        "<p>" + "宁德时代公告全文内容。" * 100 + "</p>")
    vision2 = _vision(config, html_images_enabled=True)
    reader2 = LinkReader(config, search_provider=None, vision=vision2)
    monkeypatch.setattr(
        reader2, "_fetch", lambda url: (rich_html, 200, "text/html"))
    reader2.read("l1", "https://news.example.com/a.html", profile)
    assert vision2.calls_used == 0


def test_collect_image_urls_filters_chrome(config):
    reader = LinkReader(config, search_provider=None)
    _t, _pt, _body, _tables, image_urls = reader._extract(_THIN_IMAGE_HTML)
    assert image_urls == ["https://img.example.com/announce.jpg"]


def test_fetch_pdf_url_serving_html_is_treated_as_html(config, monkeypatch):
    """'.pdf' URLs often serve an HTML hotlink-protection page; the magic
    bytes decide, not the URL suffix."""
    import mic.reader as reader_mod

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/pdf"}
        url = "https://pdf.example.com/notice.pdf"
        content = b"<script>location.href='/verify'</script>"
        text = content.decode("ascii")

    monkeypatch.setattr(reader_mod.httpx, "get",
                        lambda *a, **kw: _Resp())
    reader = LinkReader(config, search_provider=None)
    raw, status, _ctype = reader._fetch("https://pdf.example.com/notice.pdf")
    assert isinstance(raw, str)  # routed to the HTML path, not pypdf

    class _PdfResp(_Resp):
        content = b"%PDF-1.7 fake-but-magic"
        text = "ignored"

    monkeypatch.setattr(reader_mod.httpx, "get",
                        lambda *a, **kw: _PdfResp())
    raw, _s, _c = reader._fetch("https://pdf.example.com/notice.pdf")
    assert isinstance(raw, bytes)  # real PDFs still go to the PDF path
