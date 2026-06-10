"""Link Reader + Content Preprocessor (spec section 10).

Reads a search-result URL, extracts a transient analysis text (HTML or PDF),
selects the most relevant passages (including table rows), and then *discards*
the raw body. Only metadata, content hash and selected passages flow
downstream; raw content is never persisted.

Vision rescue (optional, via OpenClaw multimodal gateway): scanned PDFs with no
usable text layer are rendered to page images and transcribed; thin HTML pages
("公告截图 + 一句话" news) can contribute their main images the same way.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from mic.config import MICConfig
from mic.modeling.vision import VisionExtractor, render_pdf_pages
from mic.profile import TargetProfile
from mic.schemas import Passage
from mic.search import SearchProvider
from mic.utils import content_hash, normalize_ws, simhash

_AMOUNT_RE = re.compile(r"\d+(\.\d+)?\s*(亿元|万元|亿|万吨|吨|GWh|MWh|%|个百分点|元)")
_DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?|20\d{2}Q[1-4]|近\d+天|上半年|下半年")

_MAX_PDF_PAGES = 20
_MAX_TABLES = 5
_MAX_TABLE_ROWS = 30

# HTML image candidates for vision rescue: obvious chrome/ads are skipped by
# URL pattern; tiny images are skipped via width/height attributes when present.
_IMG_SKIP_RE = re.compile(r"logo|icon|avatar|qrcode|banner|/ad[sv]?[/_.]|\.svg|\.gif",
                          re.IGNORECASE)
_IMG_MIN_ATTR_PX = 200
_IMG_MAX_CANDIDATES = 6
_IMG_MIN_BYTES = 10_000
_IMG_MAX_BYTES = 5_000_000

# Anti-bot / CAPTCHA interstitials come back as HTTP 200 with a tiny page; if
# treated as real content they waste model calls on garbage (e.g. Baidu's
# "百度安全验证" page). Matched against the page title and a short body.
_ANTI_BOT_MARKERS = (
    "安全验证", "百度安全验证", "请输入验证码", "人机验证", "拖动滑块",
    "访问异常", "异常访问", "访问验证", "网络环境异常",
    "Just a moment", "Access Denied", "Attention Required",
    "Verifying you are human", "captcha", "CAPTCHA",
)
_ANTI_BOT_BODY_MAX_CHARS = 600  # real articles are longer than interstitials


@dataclass
class ReadResult:
    source_link_id: str
    read_status: str  # read | failed
    http_status: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    title: str | None = None
    publish_time: str | None = None
    content_hash: str | None = None
    simhash: str | None = None
    document_type: str = "html"  # html | pdf (spec 13.1)
    passages: list[Passage] = field(default_factory=list)
    failure_reason: str | None = None


class LinkReader:
    def __init__(self, config: MICConfig, search_provider: SearchProvider | None = None,
                 vision: VisionExtractor | None = None):
        self.config = config
        self.search_provider = search_provider
        self.vision = vision
        ap = config.access_profiles.get("default", {})
        self.timeout = ap.get("timeout_seconds", 15)
        self.user_agent = ap.get("user_agent", "MIC/0.3")
        self.access_profile_id = ap.get("profile_id", "default")
        gov = (config.call_governance or {}).get("budgets", {})
        self.max_passages = gov.get("max_selected_passages_per_link", 8)
        self.max_chars = gov.get("max_input_chars_per_model_call", 8000)

    def read(self, source_link_id: str, url: str, profile: TargetProfile) -> ReadResult:
        raw, http_status, ctype = self._fetch(url)
        if raw is None:
            return ReadResult(source_link_id=source_link_id, read_status="failed",
                              http_status=http_status, failure_reason="fetch_failed")
        image_texts: list[str] = []
        if isinstance(raw, bytes):
            document_type = "pdf"
            title, publish_time, body = self._extract_pdf(raw)
            tables: list[str] = []
            # Vision rescue: scanned/image-only PDF -> render pages, transcribe
            # via the multimodal gateway, continue with the transcription.
            if self.vision is not None and self.vision.available and \
                    len(body) < self.vision.min_pdf_text_chars:
                pages = render_pdf_pages(raw, self.vision.max_pdf_pages)
                rescued = self.vision.transcribe_pdf_pages(pages) if pages else None
                if rescued:
                    body = rescued
            if not body:
                return ReadResult(
                    source_link_id=source_link_id, read_status="failed",
                    http_status=http_status, content_type=ctype,
                    document_type="pdf", failure_reason="pdf_extract_failed")
        else:
            document_type = "html"
            title, publish_time, body, tables, image_urls = self._extract(raw)
            if self._is_anti_bot_page(title, body):
                return ReadResult(
                    source_link_id=source_link_id, read_status="failed",
                    http_status=http_status, content_type=ctype, title=title,
                    document_type="html", failure_reason="anti_bot_page")
            # Vision rescue: thin body + embedded images ("公告截图 + 一句话")
            # -> transcribe the main images (off by default; see config).
            if (self.vision is not None and self.vision.html_images_enabled
                    and self.vision.available and image_urls
                    and len(body) < self.vision.html_min_body_chars):
                images = self._fetch_images(image_urls, url)
                rescued = (self.vision.transcribe_page_images(images)
                           if images else None)
                if rescued:
                    image_texts.append(rescued)
        chash = content_hash(body)
        shash = simhash(body)
        passages = self._select_passages(title, body, tables, profile,
                                         image_texts=image_texts)
        return ReadResult(
            source_link_id=source_link_id, read_status="read", http_status=http_status,
            content_type=ctype, content_length=len(body), title=title,
            publish_time=publish_time, content_hash=chash, simhash=shash,
            document_type=document_type, passages=passages,
        )

    # --- fetch -------------------------------------------------------------

    def _fetch(self, url: str) -> tuple[str | bytes | None, int | None, str | None]:
        """Returns str for HTML, bytes for PDF, None on failure."""
        # Offline/mock support: synthetic providers can serve bodies directly.
        if self.search_provider is not None:
            body = self.search_provider.page_body(url)
            if body is not None:
                return body, 200, "text/html"
        try:
            resp = httpx.get(url, timeout=self.timeout, follow_redirects=True,
                             headers={"User-Agent": self.user_agent})
            ctype = resp.headers.get("content-type", "")
            if resp.status_code != 200:
                return None, resp.status_code, ctype
            looks_pdf = ("pdf" in ctype.lower()
                         or str(resp.url).lower().split("?")[0].endswith(".pdf"))
            # Trust the magic bytes over URL/Content-Type: ".pdf" URLs often
            # serve an HTML hotlink-protection/redirect page instead.
            if looks_pdf and b"%PDF" in resp.content[:1024]:
                return resp.content, resp.status_code, ctype
            return resp.text, resp.status_code, ctype
        except (httpx.HTTPError, OSError):
            return None, None, None

    # --- anti-bot detection --------------------------------------------------

    @staticmethod
    def _is_anti_bot_page(title: str, body: str) -> bool:
        """True for CAPTCHA/anti-bot interstitials served with HTTP 200.

        Requires the body to be short so long real articles that merely
        mention CAPTCHAs are not misclassified.
        """
        if len(body) > _ANTI_BOT_BODY_MAX_CHARS:
            return False
        text = f"{title} {body}"
        return any(marker in text for marker in _ANTI_BOT_MARKERS)

    # --- extraction --------------------------------------------------------

    def _extract(self, html: str) -> tuple[str, str | None, str, list[str], list[str]]:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        title = ""
        if soup.title and soup.title.string:
            title = normalize_ws(soup.title.string)
        elif soup.h1:
            title = normalize_ws(soup.h1.get_text())

        publish_time = self._guess_publish_time(soup)
        image_urls = self._collect_image_urls(soup)

        # Tables are extracted as row-joined text blocks (spec 10.1: tables are
        # high-value for metrics) and removed so rows aren't double counted.
        tables: list[str] = []
        for table in soup.find_all("table")[:_MAX_TABLES]:
            rows = []
            for tr in table.find_all("tr")[:_MAX_TABLE_ROWS]:
                cells = [normalize_ws(c.get_text()) for c in tr.find_all(["td", "th"])]
                cells = [c for c in cells if c]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                tables.append("\n".join(rows))
            table.decompose()

        blocks = []
        for el in soup.find_all(["p", "li", "h1", "h2", "h3"]):
            txt = normalize_ws(el.get_text())
            if len(txt) >= 8:
                blocks.append(txt)
        if not blocks:
            blocks = [normalize_ws(soup.get_text())]
        return title, publish_time, "\n".join(blocks), tables, image_urls

    @staticmethod
    def _collect_image_urls(soup: BeautifulSoup) -> list[str]:
        """Candidate content images for vision rescue (chrome/ads filtered)."""
        urls: list[str] = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src or src.startswith("data:") or _IMG_SKIP_RE.search(src):
                continue
            try:
                w = int(re.sub(r"\D", "", str(img.get("width") or "")) or 0)
                h = int(re.sub(r"\D", "", str(img.get("height") or "")) or 0)
            except ValueError:
                w = h = 0
            # Declared-tiny images are chrome; undeclared sizes stay candidates.
            if (w and w < _IMG_MIN_ATTR_PX) or (h and h < _IMG_MIN_ATTR_PX):
                continue
            if src not in urls:
                urls.append(src)
            if len(urls) >= _IMG_MAX_CANDIDATES:
                break
        return urls

    def _fetch_images(self, image_urls: list[str], page_url: str) -> list[bytes]:
        """Download up to max_images_per_page plausible content images."""
        limit = self.vision.max_images_per_page if self.vision else 0
        images: list[bytes] = []
        for src in image_urls:
            if len(images) >= limit:
                break
            try:
                resp = httpx.get(urljoin(page_url, src), timeout=self.timeout,
                                 follow_redirects=True,
                                 headers={"User-Agent": self.user_agent})
                if resp.status_code != 200:
                    continue
                if "image" not in resp.headers.get("content-type", ""):
                    continue
                if not (_IMG_MIN_BYTES <= len(resp.content) <= _IMG_MAX_BYTES):
                    continue
                images.append(resp.content)
            except (httpx.HTTPError, OSError):
                continue
        return images

    def _extract_pdf(self, data: bytes) -> tuple[str, str | None, str]:
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", None, ""
        try:
            reader = PdfReader(io.BytesIO(data))
        except Exception:
            return "", None, ""
        title = ""
        try:
            if reader.metadata and reader.metadata.title:
                title = normalize_ws(str(reader.metadata.title))
        except Exception:
            pass
        lines: list[str] = []
        for page in reader.pages[:_MAX_PDF_PAGES]:
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            for line in text.splitlines():
                line = normalize_ws(line)
                if len(line) >= 8:
                    lines.append(line)
        body = "\n".join(lines)
        m = _DATE_RE.search(body[:2000])
        publish_time = m.group(0) if m else None
        return title, publish_time, body

    @staticmethod
    def _guess_publish_time(soup: BeautifulSoup) -> str | None:
        for meta_name in ("article:published_time", "publishdate", "pubdate", "date"):
            tag = soup.find("meta", attrs={"property": meta_name}) or \
                  soup.find("meta", attrs={"name": meta_name})
            if tag and tag.get("content"):
                return tag["content"]
        text = soup.get_text()[:2000]
        m = _DATE_RE.search(text)
        return m.group(0) if m else None

    # --- passage selection (spec 10.2) ------------------------------------

    def _select_passages(self, title: str, body: str, tables: list[str],
                        profile: TargetProfile,
                        image_texts: list[str] | None = None) -> list[Passage]:
        entity_terms = profile.all_entity_terms()
        keyword_terms = (
            profile.products + profile.customers + profile.suppliers
            + ["客户", "供应商", "政策", "订单", "中标", "处罚", "涨价", "降价",
               "毛利率", "产能", "库存", "开工率", "风险"]
        )
        paragraphs = [p for p in body.split("\n") if p.strip()]
        scored: list[tuple[float, int, str]] = []
        for idx, para in enumerate(paragraphs):
            score = 0.0
            if idx == 0:
                score += 5  # first paragraph
            if any(t and t in para for t in entity_terms):
                score += 8
            if any(t in para for t in keyword_terms):
                score += 4
            if _AMOUNT_RE.search(para):
                score += 6
            if _DATE_RE.search(para):
                score += 3
            if any(w in para for w in ("综上", "总体", "预计", "影响", "因此")):
                score += 2
            if score > 0:
                scored.append((score, idx, para))

        scored.sort(key=lambda x: (-x[0], x[1]))
        selected = scored[: self.max_passages - 1]  # leave room for title passage
        selected.sort(key=lambda x: x[1])  # restore document order

        passages: list[Passage] = []
        if title:
            passages.append(Passage(passage_id="title", section="标题", text=title))
        budget = self.max_chars
        for _score, idx, para in selected:
            text = para[: max(0, budget)]
            if not text:
                break
            passages.append(Passage(
                passage_id=f"p{idx}", section=f"正文第{idx + 1}段", text=text))
            budget -= len(text)
            if budget <= 0:
                break

        # Tables carry dense metrics; include relevant ones within remaining
        # budget and passage cap (spec 10.2 "表格行").
        for ti, table_text in enumerate(tables, start=1):
            if budget <= 0 or len(passages) >= self.max_passages:
                break
            relevant = (
                _AMOUNT_RE.search(table_text)
                or any(t and t in table_text for t in entity_terms)
                or any(t in table_text for t in keyword_terms)
            )
            if not relevant:
                continue
            text = table_text[: max(0, budget)]
            passages.append(Passage(
                passage_id=f"t{ti}", section=f"表格{ti}", text=text))
            budget -= len(text)

        # Vision transcriptions of embedded images (e.g. announcement
        # screenshots) are included as their own passages for traceability.
        for ii, img_text in enumerate(image_texts or [], start=1):
            if budget <= 0 or len(passages) >= self.max_passages:
                break
            text = img_text[: max(0, budget)]
            passages.append(Passage(
                passage_id=f"img{ii}", section=f"图片转写{ii}", text=text))
            budget -= len(text)
        return passages
