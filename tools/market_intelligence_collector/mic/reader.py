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
    "滑动验证", "滑块验证",
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
    body_scope: dict = field(default_factory=dict)
    # Design 11: transport-level diagnostics (no page content). Legacy callers
    # may ignore these; the pipeline persists them on link_read_attempt.
    transport: str | None = None
    final_url: str | None = None
    fetch_diagnostics: dict = field(default_factory=dict)


# Parse failures after which a browser attempt may still rescue the page.
_BROWSER_RESCUE_REASONS = {"anti_bot_page", "rendering_required", "article_body_empty"}
# Parse failures that only a `allow_scope_retry` site rule may rescue.
_SCOPE_RETRY_REASONS = {"article_scope_unresolved"}
# Fetch outcomes where a second transport is pointless.
_NO_RETRY_FETCH = {"http_status", "unsupported_content", "rejected"}


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
        self.browser_fetch_cfg = dict(getattr(config, "browser_fetch", {}) or {})
        self.article_scope_version = "article_scope_v1"

    # --- public entry ---------------------------------------------------------

    def read(self, source_link_id: str, url: str, profile: TargetProfile,
             context=None, strategy: str | None = None) -> ReadResult:
        """Fetch + parse one link.

        Without ``context`` (legacy callers / tests) this is the HTTP-only path
        with the original behaviour. With a ``RunContext`` the fetch strategy
        (``http_then_browser`` / ``browser_first`` / ``http_only``) is applied
        with budget reservations and the browser session from the context.
        """
        if context is None or not context.browser_runtime.get("enabled"):
            if context is not None:
                context.budget.record("http_read_attempts")
            fetched = self._fetch_http_result(url)
            result = self._parse_fetch(source_link_id, fetched, profile)
            result.fetch_diagnostics = {"strategy": "http_only", "attempts": [fetched.diagnostics()],
                                        "parser_version": self.article_scope_version}
            return result
        return self._read_with_strategy(source_link_id, url, profile, context, strategy)

    def _plan(self, url: str, strategy: str | None) -> tuple[list[str], dict]:
        from urllib.parse import urlparse

        cfg = self.browser_fetch_cfg
        host = (urlparse(url).hostname or "").lower()
        rule = dict((cfg.get("site_rules") or {}).get(host) or {})
        mode = strategy or rule.get("mode") or cfg.get("default_mode") or "http_then_browser"
        if mode == "browser_first":
            order = ["browser", "http"]
        elif mode == "http_only":
            order = ["http"]
        elif mode == "browser_only":
            order = ["browser"]
        else:
            mode, order = "http_then_browser", ["http", "browser"]
        return order, {"mode": mode, "host": host, "allow_scope_retry": bool(rule.get("allow_scope_retry")),
                       "fallback_reasons": list(cfg.get("fallback_reasons") or ["anti_bot_page", "rendering_required"])}

    def _read_with_strategy(self, source_link_id: str, url: str, profile: TargetProfile,
                            context, strategy: str | None) -> ReadResult:
        from mic.budget import BudgetExceeded

        order, plan = self._plan(url, strategy)
        attempts: list[dict] = []
        last: ReadResult | None = None
        budget = context.budget
        for idx, transport in enumerate(order):
            counter = "http_read_attempts" if transport == "http" else "browser_read_attempts"
            try:
                budget.reserve(counter)
            except BudgetExceeded as exc:
                attempts.append({"transport": transport, "skipped": str(exc.counter), "reason": "budget"})
                break
            if transport == "http":
                fetched = self._fetch_http_result(url)
            else:
                fetched = self._fetch_browser_result(url, context)
            attempts.append(fetched.diagnostics())
            result = self._parse_fetch(source_link_id, fetched, profile)
            if result.read_status == "read":
                result.fetch_diagnostics = self._diag(plan, attempts)
                return result
            last = result
            reason = result.failure_reason or fetched.blocked_reason or "fetch_failed"
            # Authenticated retry for a blocked browser page (design 10.3).
            if transport == "browser" and reason in ("anti_bot_page", "login_required", "captcha"):
                retried = self._authenticated_retry(source_link_id, url, profile, context, fetched, attempts)
                if retried is not None:
                    if retried.read_status == "read":
                        retried.fetch_diagnostics = self._diag(plan, attempts)
                        return retried
                    last = retried
            if idx == len(order) - 1:
                break
            if not self._may_try_next(transport, fetched, result, plan):
                attempts.append({"transport": order[idx + 1], "skipped": "not_applicable", "reason": reason})
                break
        if last is None:
            last = ReadResult(source_link_id=source_link_id, read_status="failed",
                              failure_reason="fetch_failed")
        last.fetch_diagnostics = self._diag(plan, attempts)
        return last

    @staticmethod
    def _diag(plan: dict, attempts: list[dict]) -> dict:
        return {"strategy": plan["mode"], "host": plan["host"], "allow_scope_retry": plan["allow_scope_retry"],
                "attempts": attempts, "parser_version": "article_scope_v1"}

    def _may_try_next(self, transport: str, fetched, result: ReadResult, plan: dict) -> bool:
        if fetched.blocked_reason in _NO_RETRY_FETCH:
            return False  # 404/410/unsupported: no pointless retry
        if fetched.blocked_reason in ("timeout", "network_error") and transport == "http":
            return True
        reason = result.failure_reason
        if reason in _SCOPE_RETRY_REASONS:
            return transport == "http" and plan["allow_scope_retry"]
        if reason in _BROWSER_RESCUE_REASONS:
            return transport == "http" and (
                reason in plan["fallback_reasons"] or "rendering_required" in plan["fallback_reasons"]
                and reason == "article_body_empty")
        if transport == "browser" and reason in ("browser_closed", "gui_unavailable"):
            return True  # browser unavailable -> plain HTTP still worth one try
        if transport == "browser" and plan["mode"] == "browser_first":
            return fetched.blocked_reason in ("timeout", "network_error", "browser_closed")
        return False

    def _authenticated_retry(self, source_link_id: str, url: str, profile: TargetProfile, context,
                             blocked, attempts: list[dict]) -> ReadResult | None:
        from mic.budget import BudgetExceeded

        store = context.session_store
        if store is None:
            return None
        origin = store.origin_of(url)
        ok, reason = context.budget.can_authenticated_retry(origin)
        if not ok:
            attempts.append({"transport": "browser", "skipped": "authenticated_retry", "reason": reason})
            return None
        exclude = {blocked.auth_context_id} if blocked.auth_mode == "imported_cookie" else set()
        cred = store.credential_for(origin, exclude_versions=exclude)
        if cred is None:
            attempts.append({"transport": "browser", "skipped": "authenticated_retry", "reason": "no_credential"})
            return None
        try:
            context.budget.reserve_authenticated_retry(origin)
            context.budget.reserve("browser_read_attempts")
            context.browser().add_cookies(cred["cookies"], cred["credential_id"], cred["version"])
        except (BudgetExceeded, Exception) as exc:  # noqa: BLE001
            attempts.append({"transport": "browser", "skipped": "authenticated_retry",
                             "reason": f"{type(exc).__name__}"})
            return None
        finally:
            cred["cookies"] = None
        fetched = self._fetch_browser_result(url, context)
        fetched.authenticated_retry = True
        attempts.append(fetched.diagnostics())
        # Same FetchResult contract, same full body checks.
        return self._parse_fetch(source_link_id, fetched, profile)

    # --- fetch adapters -------------------------------------------------------

    def _fetch_http_result(self, url: str):
        from mic.browser.contracts import FetchResult

        if self.search_provider is not None:
            body = self.search_provider.page_body(url)
            if body is not None:
                return FetchResult(transport="mock", requested_url=url, final_url=url, http_status=200,
                                   content_type="text/html", html=body, counted_as="http_read_attempts")
        fetched = self._fetch(url)
        # ``_fetch`` reports the URL the response actually came from (after redirects);
        # review R6: filling in the requested URL left Google ``/goto`` wrappers as the
        # recorded source of articles that live on the news site.
        raw, http_status, ctype = fetched[0], fetched[1], fetched[2]
        final_url = fetched[3] if len(fetched) > 3 and fetched[3] else url
        if raw is None:
            blocked = "http_status" if http_status is not None else "network_error"
            return FetchResult(transport="http", requested_url=url, final_url=final_url if http_status else None,
                               http_status=http_status, content_type=ctype, blocked_reason=blocked,
                               counted_as="http_read_attempts")
        if isinstance(raw, bytes):
            return FetchResult(transport="http", requested_url=url, final_url=final_url, http_status=http_status,
                               content_type=ctype, content=raw, counted_as="http_read_attempts")
        return FetchResult(transport="http", requested_url=url, final_url=final_url, http_status=http_status,
                           content_type=ctype, html=raw, counted_as="http_read_attempts")

    def _fetch_browser_result(self, url: str, context):
        from mic.browser.contracts import FetchResult
        from mic.browser.fetch import BrowserFetcher
        from mic.browser.session import BrowserUnavailable

        try:
            session = context.browser()
        except BrowserUnavailable as exc:
            return FetchResult(transport="browser", requested_url=url, blocked_reason=exc.code,
                               error=str(exc)[:200], counted_as="browser_read_attempts")
        except Exception as exc:  # noqa: BLE001
            return FetchResult(transport="browser", requested_url=url, blocked_reason="browser_launch_failed",
                               error=f"{type(exc).__name__}: {str(exc)[:160]}",
                               counted_as="browser_read_attempts")
        fetcher = BrowserFetcher(session, clock=context.clock)
        try:
            return fetcher.fetch(url, context.budget.page_timeout_seconds())
        except Exception as exc:  # noqa: BLE001
            return FetchResult(transport="browser", requested_url=url, blocked_reason="browser_closed",
                               error=f"{type(exc).__name__}: {str(exc)[:160]}",
                               counted_as="browser_read_attempts")

    # --- unified parse chain (HTML / PDF / anti-bot / strict scope) -------------

    def _parse_fetch(self, source_link_id: str, fetched, profile: TargetProfile) -> ReadResult:
        base = {"transport": fetched.transport, "final_url": fetched.final_url}
        if fetched.blocked_reason is not None or (fetched.html is None and fetched.content is None):
            reason = "fetch_failed"
            if fetched.blocked_reason in ("captcha", "login_required"):
                reason = fetched.blocked_reason
            elif fetched.blocked_reason == "unsupported_content":
                reason = "unsupported_content"
            return ReadResult(source_link_id=source_link_id, read_status="failed",
                              http_status=fetched.http_status, content_type=fetched.content_type,
                              failure_reason=reason, **base)
        raw = fetched.content if fetched.content is not None else fetched.html
        result = self._parse_raw(source_link_id, raw, fetched.http_status, fetched.content_type, profile,
                                 page_url=fetched.final_url or fetched.requested_url)
        result.transport = fetched.transport
        result.final_url = fetched.final_url
        return result

    def _parse_raw(self, source_link_id: str, raw, http_status: int | None, ctype: str | None,
                   profile: TargetProfile, page_url: str = "") -> ReadResult:
        url = page_url
        image_texts: list[str] = []
        body_scope: dict = {}
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
            strict_scope = (self.config.output_schema or {}).get("limits", {}).get("strict_evidence_review") is True
            if strict_scope:
                from mic.article_scope import extract_article
                article = extract_article(raw, self)
                title, publish_time, body = article.title, article.publish_time, article.body
                tables, image_urls, body_scope = article.tables, article.image_urls, article.report
            else:
                title, publish_time, body, tables, image_urls = self._extract(raw)
            challenge_text = body_scope.pop("challenge_excerpt", "")
            if self._is_anti_bot_page(title, body or challenge_text, raw_html=raw):
                return ReadResult(
                    source_link_id=source_link_id, read_status="failed",
                    http_status=http_status, content_type=ctype, title=title,
                    document_type="html", failure_reason="anti_bot_page", body_scope=body_scope)
            if strict_scope and body_scope.get("status") != "scoped":
                return ReadResult(
                    source_link_id=source_link_id, read_status="failed",
                    http_status=http_status, content_type=ctype, title=title,
                    document_type="html", failure_reason="article_scope_unresolved", body_scope=body_scope)
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
            if strict_scope and not (body.strip() or tables or image_texts):
                return ReadResult(
                    source_link_id=source_link_id, read_status="failed",
                    http_status=http_status, content_type=ctype, title=title,
                    document_type="html", failure_reason="article_body_empty", body_scope=body_scope)
        chash = content_hash(body)
        shash = simhash(body)
        passages = self._select_passages(title, body, tables, profile,
                                         image_texts=image_texts)
        return ReadResult(
            source_link_id=source_link_id, read_status="read", http_status=http_status,
            content_type=ctype, content_length=len(body), title=title,
            publish_time=publish_time, content_hash=chash, simhash=shash,
            document_type=document_type, passages=passages, body_scope=body_scope,
        )

    # --- fetch -------------------------------------------------------------

    def _fetch(self, url: str) -> tuple[str | bytes | None, int | None, str | None, str | None]:
        """Returns (body, http_status, content_type, final_url).

        ``body`` is str for HTML, bytes for PDF, None on failure; ``final_url`` is the
        URL the response came from after redirects (None on transport failure).
        Mock/synthetic bodies are served earlier by ``_fetch_http_result``; this
        method is the plain HTTP transport only.
        """
        try:
            resp = httpx.get(url, timeout=self.timeout, follow_redirects=True,
                             headers={"User-Agent": self.user_agent})
            ctype = resp.headers.get("content-type", "")
            final_url = str(resp.url)
            if resp.status_code != 200:
                return None, resp.status_code, ctype, final_url
            looks_pdf = ("pdf" in ctype.lower()
                         or final_url.lower().split("?")[0].endswith(".pdf"))
            # Trust the magic bytes over URL/Content-Type: ".pdf" URLs often
            # serve an HTML hotlink-protection/redirect page instead.
            if looks_pdf and b"%PDF" in resp.content[:1024]:
                return resp.content, resp.status_code, ctype, final_url
            return resp.text, resp.status_code, ctype, final_url
        except (httpx.HTTPError, OSError):
            return None, None, None, None

    # --- anti-bot detection --------------------------------------------------

    @staticmethod
    def _is_anti_bot_page(title: str, body: str, raw_html: str = "") -> bool:
        """True for CAPTCHA/anti-bot interstitials served with HTTP 200.

        Requires the body to be short so long real articles that merely
        mention CAPTCHAs are not misclassified.
        """
        if len(body) > _ANTI_BOT_BODY_MAX_CHARS:
            return False
        text = f"{title} {body}"
        if any(marker in text for marker in _ANTI_BOT_MARKERS):
            return True
        # Challenge scripts may disappear during HTML text extraction.
        # Use the combined signature only for short, titleless pages.
        source = f"{body} {raw_html}".casefold()
        return (
            not title.strip()
            and "cf_app_waf" in source
            and re.search(r"\bvar\s+ac_opt\s*=", source) is not None
            and re.search(r"\bvar\s+requestinfo\s*=", source) is not None
        )

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
