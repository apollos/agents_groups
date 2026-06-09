"""Prompt construction for model tasks (spec sections 10.3, 11, 12).

Stable system prompt + schema + rubric are placed in the prefix to improve
provider prompt-cache hit rates (spec 15.4 H).
"""

from __future__ import annotations

import json
from typing import Any

from mic.profile import TargetProfile
from mic.schemas import Passage

SCHEMA_VERSION = "bundle_extraction_v0.3"

SYSTEM_PROMPT = """你是一名服务于股票/行业研究分析师的信息抽取引擎。
你的任务：阅读给定来源的标题与若干正文段落，输出严格符合 JSON Schema 的结构化分析结果。

要求：
1. 只输出一个 JSON 对象，不要输出多余文字、解释或 Markdown 代码块标记。
2. 字段使用简短值、枚举值；长解释只放在 brief 中，且控制长度。
3. 每个 fact / metric / event / relation / risk 都要给出 evidence_locator.passage_id，
   该 passage_id 必须来自输入的 selected_passages。
4. 不确定或缺失的信息放入 analyst_questions 或 coverage_gaps，不要编造。
5. decision 取值：save_structured（值得入库）| link_only（仅记录链接）| skip（无价值）。
6. 关系方向必须标准化：A 向 B 供货 => A supplier_of B，B customer_of A。
"""

SCHEMA_HINT = {
    "schema_version": SCHEMA_VERSION,
    "decision": "save_structured | link_only | skip",
    "overall_score": "0-100",
    "confidence": "0.0-1.0",
    "source_quality": {
        "source_type": "official|exchange|regulator|company|media|industry|forum|social|unknown",
        "is_original_source": "bool",
        "source_credibility_score": "0.0-1.0",
        "risk_flags": ["..."],
    },
    "brief": {
        "one_sentence": "...", "what_happened": "...", "why_it_matters": "...",
        "affected_business_lines": ["..."],
        "impact_channels": ["revenue|margin|cost|supply|demand|valuation|risk"],
        "time_horizon": "intraday|1w|1m|quarter|annual|long_term|unclear",
        "uncertainty": "...",
    },
    "facts": [{
        "fact_type": "order|sales|production|inventory|capacity|price|cost|policy|customer|supplier|risk|finance|product|technology",
        "fact_statement": "...", "entities": {"subject": "", "object": "", "product": "", "region": ""},
        "metrics": {"amount": None, "currency": None, "volume": None, "unit": None, "yoy": None, "mom": None},
        "period": "...", "direction": "positive|negative|neutral|mixed|unclear",
        "evidence_locator": {"passage_id": "p1", "section": ""}, "confidence": 0.0,
    }],
    "metrics": [{
        "metric_name": "", "metric_value": 0, "unit": "", "period": "",
        "scope": {"product": "", "region": "", "segment": None},
        "comparison": {"yoy": None, "mom": None, "wow": None},
        "interpretation": "", "impact_channels": ["..."],
        "evidence_locator": {"passage_id": "p1"}, "confidence": 0.0,
    }],
    "events": [{
        "event_type": "major_order|tender|price_change|capacity_change|policy_change|customer_change|supplier_change|risk_event|earnings_change|financing|mna|product_launch|management_change",
        "event_date": "", "summary": "",
        "entities": {"subject": "", "counterparty": "", "regulator": None, "product": ""},
        "metrics": {"amount": None, "currency": None, "capacity": None, "volume": None},
        "impact": {"direction": "positive|negative|mixed|unclear", "channels": ["..."],
                   "horizon": "1w|1m|quarter|annual|long_term", "magnitude_guess": "low|medium|high|unknown"},
        "source_corroboration_status": "single_source|multi_source|official_confirmed|conflicting",
        "evidence_locator": {"passage_id": "p1"}, "confidence": 0.0,
    }],
    "relations": [{
        "subject_entity": {"name": "", "type": "company", "ticker": None},
        "relation_type": "customer_of|supplier_of|competitor_of|partner_of|distributor_of|contractor_of|project_owner_of|regulator_of|investor_of|subsidiary_of|parent_of",
        "object_entity": {"name": "", "type": "company"},
        "qualifiers": {"product": "", "region": "", "period": "", "amount": None, "share": None, "status": "new|existing|lost|rumored|confirmed"},
        "evidence_locator": {"passage_id": "p1"}, "confidence": 0.0,
    }],
    "risks": [{
        "risk_type": "policy|legal|customer|supplier|quality|safety|environmental|liquidity|accounting|management|competition|technology|geopolitical",
        "risk_summary": "", "severity": "low|medium|high|critical",
        "time_horizon": "near_term|medium_term|long_term", "impact_channels": ["..."],
        "evidence_locator": {"passage_id": "p1"}, "confidence": 0.0,
    }],
    "catalysts": [{
        "catalyst_type": "earnings|policy_meeting|investor_day|tender_result|product_launch|capacity_commissioning|court_date|approval_deadline|conference|lockup_expiry",
        "expected_date": "", "description": "", "potential_impact": "", "confidence": 0.0,
    }],
    "customer_supplier_signals": [{
        "signal_type": "new_customer|customer_loss|customer_order|customer_cut|supplier_price_increase|supplier_disruption|certification|share_change",
        "customer_or_supplier": "", "product": "", "business_meaning": "",
        "impact_channels": ["revenue|cost|supply"], "evidence_locator": {"passage_id": "p1"},
        "confidence": 0.0,
    }],
    "price_cost_margin_signals": [{
        "signal_type": "product_price_up|product_price_down|raw_material_cost_up|raw_material_cost_down|spread_change|margin_pressure|margin_recovery",
        "product_or_material": "", "value": None, "unit": "", "period": "",
        "direction": "positive|negative|mixed|unclear",
        "evidence_locator": {"passage_id": "p1"}, "confidence": 0.0,
    }],
    "policy_signals": [{
        "policy_type": "subsidy|restriction|approval|standard|tariff|anti_dumping|export_control|environmental|safety|tax|industry_plan",
        "issuer": "", "effective_date": "", "affected_entities": ["..."],
        "affected_products": ["..."], "impact_channels": ["demand|supply|cost|capex|risk"],
        "summary": "", "evidence_locator": {"passage_id": "p1"}, "confidence": 0.0,
    }],
    "analyst_questions": [{
        "question": "", "reason": "", "priority": "high|medium|low", "suggested_queries": ["..."], "status": "open",
    }],
    "coverage_gaps": [{
        "gap_type": "missing_customer_confirmation|missing_amount|missing_date|missing_policy_detail|missing_official_source|missing_metric",
        "description": "", "suggested_next_queries": ["..."], "priority": "high|medium|low",
    }],
}


def _profile_block(profile: TargetProfile) -> dict[str, Any]:
    return {
        "target_name": profile.canonical_name,
        "type": profile.type,
        "aliases": profile.aliases,
        "products": profile.products,
        "customers": profile.customers,
        "suppliers": profile.suppliers,
    }


def build_bundle_messages(profile: TargetProfile, source_metadata: dict,
                          passages: list[Passage], output_limits: dict) -> list[dict]:
    """Messages for a single-link bundle_extraction call."""
    user_payload = {
        "task": "bundle_extraction",
        "schema_version": SCHEMA_VERSION,
        "target_profile": _profile_block(profile),
        "source_metadata": source_metadata,
        "selected_passages": [p.model_dump() for p in passages],
        "required_output": ["brief", "facts", "metrics", "events", "relations",
                            "risks", "catalysts", "customer_supplier_signals",
                            "price_cost_margin_signals", "policy_signals",
                            "analyst_questions", "coverage_gaps"],
        "output_limits": output_limits,
        "output_schema_hint": SCHEMA_HINT,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


BATCH_TRIAGE_SYSTEM = """你是搜索结果初筛器。给定多条搜索结果（标题+摘要），
对每条判断是否值得读取与是否需要模型深入分析。只输出 JSON。"""


def build_batch_triage_messages(items: list[dict]) -> list[dict]:
    payload = {
        "task": "serp_batch_triage",
        "items": items,
        "output_schema_hint": {
            "results": [{"id": "hit_x", "triage_decision": "read|link_record_only|skip_for_now",
                         "read_priority": 0, "need_model": True}],
        },
    }
    return [
        {"role": "system", "content": BATCH_TRIAGE_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


ARBITRATION_SYSTEM = SYSTEM_PROMPT + """
你正在执行【仲裁】任务：之前多个模型对同一来源的抽取结果在某些字段上存在冲突。
请重新独立判断，重点解决列出的冲突字段（如金额、客户、事件日期、影响方向、关系方向），
给出你认为最可信的取值，并据此输出完整 bundle JSON。无法确定的冲突应在
analyst_questions 中标注需人工/官方确认。"""


def build_arbitration_messages(profile: TargetProfile, source_metadata: dict,
                               passages: list[Passage], output_limits: dict,
                               conflicts: list[dict]) -> list[dict]:
    """Messages for an arbitration call over a single link's conflicting output."""
    user_payload = {
        "task": "arbitration",
        "schema_version": SCHEMA_VERSION,
        "target_profile": _profile_block(profile),
        "source_metadata": source_metadata,
        "selected_passages": [p.model_dump() for p in passages],
        "field_conflicts": conflicts,
        "output_limits": output_limits,
        "output_schema_hint": SCHEMA_HINT,
    }
    return [
        {"role": "system", "content": ARBITRATION_SYSTEM},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
