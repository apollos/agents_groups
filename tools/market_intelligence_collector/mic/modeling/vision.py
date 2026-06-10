"""Vision rescue extraction via a multimodal gateway (default: OpenClaw).

The LinkReader uses this as a rescue path, not a primary one:

- scanned PDFs (no usable text layer) get their first pages rendered to images
  and transcribed by the configured multimodal model;
- image-heavy HTML pages with very thin bodies (e.g. "公告截图 + 一句话" news)
  can contribute their main images the same way (off by default).

Protocol note: OpenClaw's OpenAI-compatible surface routes images differently
per endpoint. ``/v1/chat/completions`` turns ``image_url`` parts into agent
*attachments*, which a text-only primary model never sees. ``/v1/responses``
with ``input_image`` (base64 source) puts the image into the model prompt, and
the ``x-openclaw-model`` header forces a vision-capable backend for the call.
We therefore speak the Responses API here. Transcribed text flows through the
normal passage-selection pipeline; the raw images stay transient - nothing
binary is ever persisted.
"""

from __future__ import annotations

import base64
import logging
import os

import httpx

from mic.config import MICConfig
from mic.modeling.adapter import ModelRegistry

logger = logging.getLogger("mic.vision")

_PDF_PROMPT = (
    "你是文档转写助手。下面是一份 PDF 文档（如公司公告、研报、招中标文件）的"
    "页面截图。请把页面中的全部正文文字按阅读顺序完整转写为纯文本：表格转写为"
    "'单元格1 | 单元格2' 的行格式；保留所有数字、金额、日期、公司名；忽略页眉、"
    "页脚、水印。只输出转写文本，不要任何解释或评论。"
)

_IMAGE_PROMPT = (
    "你是财经信息提取助手。下面是新闻页面正文中嵌入的图片（可能是公告截图、"
    "数据图表或表格）。请把图片中可读的文字与数据完整转写为纯文本：表格转写为"
    "'单元格1 | 单元格2' 的行格式；图表请描述其标题、坐标轴和关键数值。如果图片"
    "不含有效信息（如广告、二维码、装饰图），只输出'无有效信息'。只输出转写"
    "内容，不要任何解释。"
)


class VisionExtractor:
    """Budgeted wrapper around the multimodal adapter configured in
    call_governance.yaml -> vision_extract."""

    def __init__(self, config: MICConfig, registry: ModelRegistry | None = None):
        vcfg = (config.call_governance or {}).get("vision_extract", {})
        self.enabled = vcfg.get("enabled", True)
        self.model_config_id = vcfg.get("model_config_id", "openclaw_research")
        self.max_pdf_pages = vcfg.get("max_pdf_pages", 5)
        self.min_pdf_text_chars = vcfg.get("min_pdf_text_chars", 200)
        self.html_images_enabled = vcfg.get("html_images_enabled", False)
        self.html_min_body_chars = vcfg.get("html_min_body_chars", 500)
        self.max_images_per_page = vcfg.get("max_images_per_page", 3)
        self.max_calls_per_run = vcfg.get("max_calls_per_run", 10)
        # Responses-API specifics: which agent route to request and which
        # backend to force via the x-openclaw-model header. The override must
        # name a text+image model on the OpenClaw side; without it the
        # gateway's default (often text-only) model would receive the request
        # and never see the images. OPENCLAW_VISION_MODEL (.env) wins over
        # the yaml value since model names are deployment-specific.
        self.request_model = vcfg.get("request_model", "openclaw/default")
        self.model_override = (os.environ.get("OPENCLAW_VISION_MODEL")
                               or vcfg.get("model_override", ""))
        self.timeout = vcfg.get("timeout_seconds", 120)
        self.max_output_tokens = vcfg.get("max_output_tokens", 4096)
        registry = registry or ModelRegistry(config)
        # The registry entry supplies endpoint + token; the actual HTTP call
        # uses the Responses API rather than the adapter's chat-completions.
        self.adapter = registry.get(self.model_config_id)
        self.calls_used = 0
        self.estimated_cost = 0.0

    # --- run lifecycle -------------------------------------------------------

    def reset_run(self) -> None:
        """Pipeline instances outlive a single run; budgets are per run."""
        self.calls_used = 0
        self.estimated_cost = 0.0

    @property
    def available(self) -> bool:
        return (self.enabled and self.adapter is not None
                and bool(self.adapter.api_key)
                and self.calls_used < self.max_calls_per_run)

    # --- transcription ---------------------------------------------------------

    def transcribe_pdf_pages(self, images: list[bytes]) -> str | None:
        return self._transcribe(images, _PDF_PROMPT, kind="pdf")

    def transcribe_page_images(self, images: list[bytes]) -> str | None:
        text = self._transcribe(images, _IMAGE_PROMPT, kind="html_image")
        if text and "无有效信息" in text and len(text) < 30:
            return None
        return text

    def _transcribe(self, images: list[bytes], prompt: str, kind: str) -> str | None:
        if not images or not self.available:
            return None
        content: list[dict] = [{"type": "input_text", "text": prompt}]
        for img in images:
            content.append({
                "type": "input_image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(img).decode("ascii"),
                },
            })
        payload = {
            "model": self.request_model,
            "input": [{"type": "message", "role": "user", "content": content}],
            "max_output_tokens": self.max_output_tokens,
        }
        headers = {"Authorization": f"Bearer {self.adapter.api_key}"}
        if self.model_override:
            headers["x-openclaw-model"] = self.model_override
        url = self.adapter.endpoint.rstrip("/") + "/responses"
        self.calls_used += 1
        try:
            resp = httpx.post(url, json=payload, headers=headers,
                              timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("vision_extract_failed kind=%s url=%s err=%s",
                           kind, url, str(exc)[:300])
            return None
        texts = [c.get("text", "")
                 for item in data.get("output", [])
                 for c in (item.get("content") or [])
                 if c.get("type") == "output_text"]
        text = "\n".join(t for t in texts if t).strip()
        if not text:
            logger.warning("vision_extract_empty kind=%s status=%s",
                           kind, data.get("status"))
            return None
        logger.info("vision_extract_ok kind=%s images=%d chars=%d",
                    kind, len(images), len(text))
        return text


def render_pdf_pages(data: bytes, max_pages: int, scale: float = 2.0) -> list[bytes]:
    """Render the first pages of a PDF to JPEG bytes via pypdfium2.

    Returns [] when the renderer is unavailable or the document is unreadable;
    callers treat that as "vision rescue not possible".
    """
    try:
        import io

        import pypdfium2 as pdfium
    except ImportError:
        logger.warning("pypdfium2 not installed; cannot render scanned PDF pages")
        return []
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:  # noqa: BLE001 - malformed document
        return []
    images: list[bytes] = []
    try:
        for i in range(min(len(pdf), max_pages)):
            try:
                bitmap = pdf[i].render(scale=scale)
                pil = bitmap.to_pil().convert("RGB")
                buf = io.BytesIO()
                pil.save(buf, format="JPEG", quality=85)
                images.append(buf.getvalue())
            except Exception:  # noqa: BLE001 - skip unrenderable page
                continue
    finally:
        pdf.close()
    return images
