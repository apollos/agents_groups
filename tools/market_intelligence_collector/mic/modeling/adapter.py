"""Model Adapter Layer (spec section 3 + 11.2).

OpenAI-compatible adapter that fronts DeepSeek / Qwen-DashScope / SiliconFlow /
OpenClaw Gateway uniformly. When no API key is available and MIC_ALLOW_MOCK is
on, a deterministic local mock produces a schema-valid bundle from the input
passages so the pipeline runs offline.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from mic.config import MICConfig

try:  # openai is optional at runtime when only the mock is used.
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(亿元|万元|亿|万吨|吨|GWh|MWh)")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(%|个百分点)")


@dataclass
class ModelCallResult:
    model_config_id: str
    provider: str
    provider_type: str
    model_name: str
    status: str  # success | request_failed | json_invalid
    raw_text: str = ""
    parsed: dict | None = None
    input_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    estimated_cost: float = 0.0
    error_type: str | None = None
    error_message: str | None = None
    is_mock: bool = False


@dataclass
class ModelAdapter:
    model_config_id: str
    provider: str
    provider_type: str
    endpoint: str
    model: str
    api_key: str
    enabled: bool = True
    pricing: dict[str, float] = field(default_factory=dict)
    allow_mock: bool = True
    json_output: bool = True
    max_output_tokens: int = 4096
    _client: Any = field(default=None, repr=False, compare=False)

    @property
    def usable(self) -> bool:
        return self.enabled and (bool(self.api_key) or self.allow_mock)

    def complete(self, messages: list[dict], max_tokens: int | None = None,
                 json_mode: bool | None = None) -> ModelCallResult:
        """``json_mode`` overrides the registry's json_output capability for a
        single call - vision transcription wants plain text, not a JSON bundle.
        """
        input_chars = sum(len(m.get("content", "")) for m in messages)
        if not self.api_key or OpenAI is None:
            if self.allow_mock:
                return self._mock_complete(messages, input_chars)
            return ModelCallResult(
                model_config_id=self.model_config_id, provider=self.provider,
                provider_type=self.provider_type, model_name=self.model,
                status="request_failed", input_chars=input_chars,
                error_type="no_api_key", error_message="API key missing and mock disabled",
            )
        return self._api_complete(messages, max_tokens or self.max_output_tokens,
                                  input_chars, json_mode=json_mode)

    # --- real API ----------------------------------------------------------

    def _api_complete(self, messages: list[dict], max_tokens: int,
                     input_chars: int, json_mode: bool | None = None) -> ModelCallResult:
        if self._client is None:
            self._client = OpenAI(api_key=self.api_key, base_url=self.endpoint)
        client = self._client
        start = time.time()
        want_json = self.json_output if json_mode is None else json_mode
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages,
                                  "max_tokens": max_tokens, "temperature": 0.2}
        if want_json:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surface as failure for fallback
            return ModelCallResult(
                model_config_id=self.model_config_id, provider=self.provider,
                provider_type=self.provider_type, model_name=self.model,
                status="request_failed", input_chars=input_chars,
                latency_ms=int((time.time() - start) * 1000),
                error_type=type(exc).__name__, error_message=str(exc)[:500],
            )
        latency_ms = int((time.time() - start) * 1000)
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", 0) or 0
        out_tok = getattr(usage, "completion_tokens", 0) or 0
        # Cached prompt tokens: OpenAI-style prompt_tokens_details.cached_tokens,
        # DeepSeek-style prompt_cache_hit_tokens (spec 15.4 H).
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        if not cached:
            cached = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        reasoning = 0
        out_details = getattr(usage, "completion_tokens_details", None)
        if out_details is not None:
            reasoning = getattr(out_details, "reasoning_tokens", 0) or 0
        result = ModelCallResult(
            model_config_id=self.model_config_id, provider=self.provider,
            provider_type=self.provider_type, model_name=self.model, status="success",
            raw_text=text, input_chars=input_chars, input_tokens=in_tok,
            output_tokens=out_tok, reasoning_tokens=reasoning, cached_tokens=cached,
            latency_ms=latency_ms,
        )
        result.estimated_cost = self._estimate_cost(in_tok, out_tok)
        if want_json:
            result.parsed = self._parse_json(text)
            if result.parsed is None:
                result.status = "json_invalid"
        return result

    def _estimate_cost(self, in_tok: int, out_tok: int) -> float:
        pin = self.pricing.get("input", 0.0)
        pout = self.pricing.get("output", 0.0)
        return round((in_tok * pin + out_tok * pout) / 1_000_000, 6)

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(json)?", "", text).rstrip("`").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    return None
            return None

    # --- deterministic mock ------------------------------------------------

    def _mock_complete(self, messages: list[dict], input_chars: int) -> ModelCallResult:
        user = next((m["content"] for m in messages if m["role"] == "user"), "{}")
        try:
            payload = json.loads(user)
        except json.JSONDecodeError:
            payload = {}
        if payload.get("task") == "serp_batch_triage":
            parsed = self._mock_batch_triage(payload)
        else:
            parsed = self._mock_bundle(payload)
        text = json.dumps(parsed, ensure_ascii=False)
        out_tokens = max(1, len(text) // 3)
        return ModelCallResult(
            model_config_id=self.model_config_id, provider=self.provider,
            provider_type=self.provider_type, model_name=self.model + "-mock",
            status="success", raw_text=text, parsed=parsed, input_chars=input_chars,
            input_tokens=input_chars // 3, output_tokens=out_tokens,
            latency_ms=2, estimated_cost=0.0, is_mock=True,
        )

    @staticmethod
    def _mock_batch_triage(payload: dict) -> dict:
        results = []
        for item in payload.get("items", []):
            text = f"{item.get('title','')}{item.get('snippet','')}"
            need = any(k in text for k in ("订单", "中标", "亿元", "处罚", "涨价"))
            results.append({
                "id": item.get("id"),
                "triage_decision": "read" if need else "link_record_only",
                "read_priority": 70 if need else 40, "need_model": need,
            })
        return {"results": results}

    def _mock_bundle(self, payload: dict) -> dict:
        profile = payload.get("target_profile", {})
        target_name = profile.get("target_name", "")
        aliases = profile.get("aliases", [])
        customers = profile.get("customers", [])
        passages = payload.get("selected_passages", [])
        meta = payload.get("source_metadata", {})

        joined = " ".join(p.get("text", "") for p in passages)
        first_pid = passages[0]["passage_id"] if passages else "title"

        # Deterministic per-model jitter so ensemble outputs differ slightly.
        seed = sum(ord(c) for c in self.model_config_id) % 7
        conf = round(min(0.95, 0.55 + 0.05 * (seed % 4)), 2)

        amount, unit = None, None
        m = _AMOUNT_RE.search(joined)
        if m:
            amount = float(m.group(1))
            unit = m.group(2)

        facts, events, metrics, relations, risks, catalysts = [], [], [], [], [], []

        def passage_for(keyword: str) -> str:
            for p in passages:
                if keyword in p.get("text", ""):
                    return p["passage_id"]
            return first_pid

        if "供货" in joined or "订单" in joined or "中标" in joined or "合同" in joined:
            cust = next((c for c in customers if c in joined), customers[0] if customers else "客户")
            pid = passage_for("供货" if "供货" in joined else "订单")
            events.append({
                "event_type": "major_order", "event_date": meta.get("publish_time", ""),
                "summary": f"{target_name or '公司'}与{cust}签订供货/订单协议",
                "entities": {"subject": target_name, "counterparty": cust, "product": ""},
                "metrics": {"amount": amount, "currency": "CNY" if unit else None,
                            "capacity": None, "volume": None},
                "impact": {"direction": "positive", "channels": ["revenue"],
                           "horizon": "quarter", "magnitude_guess": "medium"},
                "source_corroboration_status": "single_source",
                "evidence_locator": {"passage_id": pid}, "confidence": conf,
            })
            relations.append({
                "subject_entity": {"name": target_name, "type": "company"},
                "relation_type": "supplier_of",
                "object_entity": {"name": cust, "type": "company"},
                "qualifiers": {"product": "", "period": "", "status": "confirmed",
                               "amount": amount},
                "evidence_locator": {"passage_id": pid}, "confidence": conf,
            })
            facts.append({
                "fact_type": "order", "fact_statement": f"{target_name}获得来自{cust}的订单",
                "entities": {"subject": target_name, "object": cust},
                "metrics": {"amount": amount, "currency": "CNY" if unit else None,
                            "volume": None, "unit": unit, "yoy": None, "mom": None},
                "period": meta.get("publish_time", ""), "direction": "positive",
                "evidence_locator": {"passage_id": pid}, "confidence": conf,
            })

        pm = _PCT_RE.search(joined)
        if "开工率" in joined or "产能利用率" in joined:
            metrics.append({
                "metric_name": "开工率", "metric_value": float(pm.group(1)) if pm else None,
                "unit": "%", "period": meta.get("publish_time", ""),
                "scope": {"product": "", "region": "中国"},
                "comparison": {"yoy": None, "mom": None}, "interpretation": "产能利用率回升",
                "impact_channels": ["supply"], "evidence_locator": {"passage_id": passage_for("开工率")},
                "confidence": conf,
            })
        if "毛利率" in joined:
            metrics.append({
                "metric_name": "毛利率", "metric_value": None, "unit": "%",
                "period": meta.get("publish_time", ""), "scope": {},
                "comparison": {"mom": float(pm.group(1)) if pm else None},
                "interpretation": "毛利率环比改善", "impact_channels": ["margin"],
                "evidence_locator": {"passage_id": passage_for("毛利率")}, "confidence": conf,
            })

        if "处罚" in joined or "事故" in joined or "诉讼" in joined:
            risks.append({
                "risk_type": "environmental" if "环保" in joined else "legal",
                "risk_summary": "公司涉及监管处罚/风险事件", "severity": "medium",
                "time_horizon": "near_term", "impact_channels": ["risk"],
                "evidence_locator": {"passage_id": passage_for("处罚")}, "confidence": conf,
            })

        if "财报" in joined or "业绩说明会" in joined:
            catalysts.append({
                "catalyst_type": "earnings", "expected_date": "",
                "description": "临近财报/业绩说明会", "potential_impact": "可能修正预期",
                "confidence": conf,
            })

        cs_signals, pcm_signals, policy_signals = [], [], []
        if "供货" in joined or "订单" in joined or "客户" in joined:
            cust = next((c for c in customers if c in joined),
                        customers[0] if customers else "客户")
            cs_signals.append({
                "signal_type": "customer_order" if ("订单" in joined or "供货" in joined)
                else "new_customer",
                "customer_or_supplier": cust, "product": "",
                "business_meaning": f"与{cust}的客户关系/订单出现变化",
                "impact_channels": ["revenue"],
                "evidence_locator": {"passage_id": passage_for("客户")}, "confidence": conf,
            })
        if "涨价" in joined or "降价" in joined or "价格" in joined or "毛利率" in joined:
            up = "涨价" in joined or ("价格" in joined and "下降" not in joined)
            pcm_signals.append({
                "signal_type": "raw_material_cost_down" if "碳酸锂" in joined and "下降" in joined
                else ("product_price_up" if up else "product_price_down"),
                "product_or_material": (profile.get("products", ["产品"]) or ["产品"])[0],
                "value": float(pm.group(1)) if pm else None,
                "unit": pm.group(2) if pm else None, "period": meta.get("publish_time", ""),
                "direction": "positive" if up else "negative",
                "evidence_locator": {"passage_id": passage_for("价格")}, "confidence": conf,
            })
        if "政策" in joined or "补贴" in joined or "关税" in joined or "环保" in joined:
            policy_signals.append({
                "policy_type": "tariff" if "关税" in joined else
                ("environmental" if "环保" in joined else
                 ("subsidy" if "补贴" in joined else "industry_plan")),
                "issuer": "", "effective_date": "",
                "affected_entities": [target_name] if target_name else [],
                "affected_products": profile.get("products", [])[:2],
                "impact_channels": ["demand", "cost"],
                "summary": "相关政策/监管变化", "evidence_locator": {"passage_id": passage_for("政策")},
                "confidence": conf,
            })

        # Decision based on what we found.
        has_signal = bool(facts or events or metrics or risks or cs_signals
                          or pcm_signals or policy_signals)
        decision = "save_structured" if has_signal else "link_only"
        overall = 75 if has_signal else 40

        questions = []
        if amount is None and (events or facts):
            questions.append({
                "question": "该订单/事件的具体金额是否有官方公告确认？",
                "reason": "来源提到订单/事件但金额不明确。", "priority": "high",
                "suggested_queries": [f"{aliases[0] if aliases else target_name} 订单 金额 公告"],
                "status": "open",
            })

        gaps = []
        if events and not any("official" in str(meta.get("source_type", "")) for _ in [0]):
            gaps.append({
                "gap_type": "missing_official_source",
                "description": "事件缺少交易所/监管官方来源交叉验证。",
                "suggested_next_queries": [f"{target_name} 公告"], "priority": "medium",
            })

        return {
            "schema_version": "bundle_extraction_v0.3",
            "source_link_id": meta.get("source_link_id"),
            "decision": decision, "overall_score": overall, "confidence": conf,
            "source_quality": {
                "source_type": meta.get("source_type", "media"),
                "is_original_source": meta.get("source_type") in ("exchange", "regulator"),
                "source_credibility_score": 0.8 if meta.get("source_type") in
                ("exchange", "regulator") else 0.5, "risk_flags": [],
            },
            "brief": {
                "one_sentence": (passages[0]["text"][:60] if passages else ""),
                "what_happened": (joined[:160]),
                "why_it_matters": "可能影响收入/毛利/供需，需结合官方来源确认。",
                "affected_business_lines": profile.get("products", [])[:2],
                "impact_channels": ["revenue", "margin"] if has_signal else [],
                "time_horizon": "quarter", "uncertainty": "金额/日期/客户待确认",
            },
            "facts": facts, "metrics": metrics, "events": events,
            "relations": relations, "risks": risks, "catalysts": catalysts,
            "customer_supplier_signals": cs_signals,
            "price_cost_margin_signals": pcm_signals, "policy_signals": policy_signals,
            "analyst_questions": questions, "coverage_gaps": gaps,
        }


class ModelRegistry:
    """Builds adapters from model_registry.yaml."""

    def __init__(self, config: MICConfig):
        self.config = config
        self.adapters: dict[str, ModelAdapter] = {}
        self._build()

    def _build(self) -> None:
        registry = self.config.model_registry
        models = registry.get("models", {})
        default_max_out = registry.get("default_max_output_tokens", 4096)
        pricing = self.config.pricing_hints
        for mid, spec in models.items():
            api_key = os.environ.get(spec.get("api_key_env", ""), "")
            self.adapters[mid] = ModelAdapter(
                model_config_id=mid, provider=spec.get("provider", ""),
                provider_type=spec.get("provider_type", "openai_compatible_direct"),
                endpoint=spec.get("endpoint", ""), model=spec.get("model", ""),
                api_key=api_key, enabled=spec.get("enabled", True),
                pricing=pricing.get(mid, {}), allow_mock=self.config.allow_mock,
                json_output=spec.get("capabilities", {}).get("json_output", True),
                max_output_tokens=spec.get("max_output_tokens", default_max_out),
            )

    def get(self, model_config_id: str) -> ModelAdapter | None:
        return self.adapters.get(model_config_id)
