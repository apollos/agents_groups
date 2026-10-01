"""Opt-in necessary-evidence review, not a general semantic truth verifier.

Keeps observations separate from totals, supports literal dates and explicit
additive quantities, and quarantines claims lacking category-specific evidence.
No entity alias guessing or global date borrowing. A source price consistency
check is conditional on equal scope; it never rewrites the source's quotation.
Unknown phrasing may be held for review. Raw model output must be retained.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from mic.money import cny_amount_supported
from mic.relation_evidence import _title
from mic.schemas import CoverageGap

NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
PRICE_UNITS = ("元/Wh", "元/kWh", "元/MWh", "元/吨", "元/公斤", "元/千克", "元/件")
DATE = re.compile(r"(?<!\d)(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?(?!\d)")
INFERENCE = re.compile(r"成本|毛利|盈利|竞争格局|经济性|替代路径|cost|margin|profit", re.I)
NEGATIVE_OR_HYPOTHETICAL = re.compile(r"并未|并非|没有|未见|尚未|不曾|未发生|未下降|未上升|未上涨|未下跌|否认|传闻|可能|或许|假如|如果|疑似|疑为|预计")


def decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def number(value):
    return int(value) if value == value.to_integral_value() else float(value)


def numeric_quote(text, value, unit):
    wanted = decimal(value)
    if wanted is None or not unit:
        return None
    pattern = rf"(?<![\d.,+\-])({NUMBER})\s*{re.escape(unit)}(?![A-Za-z/／])"
    for match in re.finditer(pattern, text):
        if decimal(match.group(1)) == wanted:
            return match.group(0)
    return None


def dates_in(text):
    found = {}
    for match in DATE.finditer(text):
        try:
            normalized = date(*(int(x) for x in match.groups())).isoformat()
        except ValueError:
            continue
        found.setdefault(normalized, []).append(match.group(0))
    return found


def date_supported(text, value):
    return bool(value and value in dates_in(text))


def additive_quantity(text, value, unit):
    """Only an explicit '+' joins operands; no arbitrary subset sums.

    Supports MW/MWh pairs used in capacity descriptions. Rejects passages with
    total/subset/alternative wording; quantities in other passages are ignored.
    """
    wanted = decimal(value)
    if unit not in ("MW", "MWh") or wanted is None:
        return None
    if re.search(r"其中|含其中|包括其中|或|备选|替代|总计|合计|总规模", text):
        return None
    atom = rf"(?:{NUMBER}\s*MW\s*/\s*)?{NUMBER}\s*MWh" if unit == "MWh" else rf"{NUMBER}\s*MW(?:\s*/\s*{NUMBER}\s*MWh)?"
    pattern = rf"(?<![\d.]){atom}(?:[^+。；;\n]{{0,24}}\+\s*{atom})+"
    matches = []
    for match in re.finditer(pattern, text):
        parts = match.group(0).split("+")
        operands = []
        for part in parts:
            token = re.search(rf"(?<![\d.])({NUMBER})\s*{unit}(?![A-Za-z])", part)
            if token is None:
                break
            operands.append(decimal(token.group(1)))
        if len(operands) == len(parts) and sum(operands, Decimal(0)) == wanted:
            matches.append({"kind": "explicit_sum", "operator": "+",
                            "operands": [number(v) for v in operands],
                            "value": number(wanted), "unit": unit, "quote": match.group(0)})
    return matches[0] if len(matches) == 1 else None


def quantity_supported(text, value, unit):
    return numeric_quote(text, value, unit) is not None or additive_quantity(text, value, unit) is not None


class EvidenceReview:
    def __init__(self, bundle, passages, warnings):
        self.bundle, self.warnings = bundle, warnings
        counts = Counter(p.passage_id for p in passages)
        self.body = {p.passage_id: p.text for p in passages
                     if counts[p.passage_id] == 1 and not _title(p) and p.text.strip()}
        self.items = []
        self.descriptions = {g.description for g in bundle.coverage_gaps}

    def text(self, item):
        return self.body.get(item.evidence_locator.passage_id, "")

    def record(self, path, reason, original, action="pending_review", detail=None):
        item = {"path": path, "reason": reason, "original": original,
                "action": action, "detail": detail or {}}
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        item["review_id"] = "quality-" + hashlib.sha256(encoded.encode()).hexdigest()[:16]
        self.items.append(item)
        self.warnings.append(f"quality review: {path}; {reason}; {item['review_id']}")
        description = f"待核查 {path}：{reason}；review_id={item['review_id']}"
        if description not in self.descriptions:
            self.bundle.coverage_gaps.append(CoverageGap(
                gap_type="extraction_quality_review", description=description, priority="high"))
            self.descriptions.add(description)

    def separate_prices(self):
        for index, fact in enumerate(self.bundle.facts):
            fields = fact.metrics
            unit = fields.get("unit_price_unit") or fields.get("unit")
            if fact.fact_type != "price" or unit not in PRICE_UNITS:
                continue
            amount, existing = fields.get("amount"), fields.get("unit_price")
            if amount is None:
                continue
            quote = numeric_quote(self.text(fact), amount, unit)
            conflict = existing is not None and decimal(existing) != decimal(amount)
            # CNY only; an unqualified currency must not be silently guessed.
            cny = str(fields.get("currency") or "").upper() in ("CNY", "RMB", "人民币")
            if not quote or conflict or not cny or fields.get("amount_unit"):
                continue  # unresolved money is quarantined after normalization
            fields.update({"amount": None, "unit_price": amount, "unit_price_unit": unit,
                           "unit_price_evidence": {"passage_id": fact.evidence_locator.passage_id,
                                                   "quote": quote},
                           "amount_reclassified_as": "unit_price"})

    def check_money(self):
        for group, attr in (("facts", "metrics"), ("events", "metrics"), ("relations", "qualifiers")):
            for index, item in enumerate(getattr(self.bundle, group)):
                fields = getattr(item, attr)
                amount = fields.get("amount")
                if amount is None or cny_amount_supported(fields, self.text(item)):
                    continue
                self.record(f"{group}[{index}].{attr}.amount", "金额或币种缺少可验证依据",
                            dict(fields), "clear_amount")
                fields["amount_candidate"] = amount
                fields["amount_status"] = "pending_review"
                fields["amount"] = None
        for index, fact in enumerate(self.bundle.facts):
            fields = fact.metrics
            value, unit = fields.get("unit_price"), fields.get("unit_price_unit")
            cny = str(fields.get("currency") or "").upper() in ("CNY", "RMB", "人民币")
            if value is not None and (not cny or unit not in PRICE_UNITS or not numeric_quote(self.text(fact), value, unit)):
                self.record(f"facts[{index}].metrics.unit_price", "单价缺少数值与单位的正文依据", value, "clear_price")
                fields.update(unit_price_candidate=value, unit_price=None, unit_price_status="pending_review")

    def check_fields(self):
        for group, date_attr in (("events", "event_date"), ("facts", "period"), ("metrics", "period")):
            for index, item in enumerate(getattr(self.bundle, group)):
                text = self.text(item)
                value = getattr(item, date_attr)
                # Restrict this check to full dates; year/quarter periods require
                # a separate period resolver and are not silently relabeled.
                if value and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) and not date_supported(text, value):
                    candidates = [{"passage_id": pid, "quotes": dates_in(body)[value]}
                                  for pid, body in self.body.items() if value in dates_in(body)]
                    self.record(f"{group}[{index}].{date_attr}", "日期跨段归属未验证" if candidates else "日期未获正文支持",
                                value, "clear_date", {"candidate_passages": candidates})
                    container = item.scope if group == "metrics" else item.metrics
                    container[date_attr + "_review"] = {"candidate": value, "status": "pending_review",
                                                       "candidate_passages": candidates}
                    setattr(item, date_attr, None)
        for index, event in enumerate(self.bundle.events):
            counterparty = event.entities.get("counterparty")
            if counterparty and str(counterparty) not in self.text(event):
                self.record(f"events[{index}].entities.counterparty", "交易对手角色或跨段归属未验证",
                            counterparty, "clear_counterparty")
                event.entities.update(counterparty=None, counterparty_candidate=counterparty,
                                      counterparty_status="pending_review")
        for metric in self.bundle.metrics:
            derived = additive_quantity(self.text(metric), metric.metric_value, metric.unit)
            if derived:
                metric.scope["value_evidence"] = {**derived, "passage_id": metric.evidence_locator.passage_id}
            elif "value_evidence" in metric.scope:
                # Never trust a model-supplied arithmetic certificate.
                metric.scope.pop("value_evidence")

    def quarantine_signals(self):
        # Necessary textual cues only. Passing these filters is NOT a semantic
        # entailment certificate. Unsupported phrasing goes to review.
        required = {
            "share_change": (r"份额|占比|market share", r"上升|下降|提高|降低|增至|降至|同比|环比|increased|decreased"),
            "product_price_up": (r"价格|单价|售价|price", r"上涨|上调|涨价|提高|升至|increased|rose"),
            "product_price_down": (r"价格|单价|售价|price", r"下降|下调|降价|降低|降至|decreased|fell"),
            "raw_material_cost_up": (r"原料|原材料|material", r"上涨|提高|增加|升至|increased|rose"),
            "raw_material_cost_down": (r"原料|原材料|material", r"下降|降低|减少|降至|decreased|fell"),
            "spread_change": (r"价差|利差|spread", r"扩大|缩小|收窄|走阔|同比|环比|widened|narrowed"),
            "margin_pressure": (r"毛利|利润率|盈利|margin|profit", r"下降|承压|压缩|减少|降低|pressure|decreased"),
            "margin_recovery": (r"毛利|利润率|盈利|margin|profit", r"回升|恢复|改善|提高|recovery|improved"),
        }
        for group in ("customer_supplier_signals", "price_cost_margin_signals", "policy_signals"):
            kept = []
            for index, signal in enumerate(getattr(self.bundle, group)):
                text, reason = self.text(signal), None
                if not text:
                    reason = "缺少可定位的非标题正文"
                elif group == "customer_supplier_signals" and (
                        not signal.customer_or_supplier or signal.customer_or_supplier not in text):
                    reason = "客户供应商身份或角色缺少正文支持"
                elif group == "policy_signals" and (not signal.issuer or signal.issuer not in text
                                                    or NEGATIVE_OR_HYPOTHETICAL.search(signal.issuer)
                                                    or not re.search(r"发布|印发|政策|规划|条例|办法|通知|issued|regulation", text, re.I)):
                    reason = "政策发布方未明确，不能由试点项目推定政策"
                kind = getattr(signal, "signal_type", "")
                if not reason and kind in required:
                    cues = required[kind]
                    if NEGATIVE_OR_HYPOTHETICAL.search(text) or not all(re.search(cue, text, re.I) for cue in cues):
                        reason = "缺少该趋势、份额或利润判断的必要证据"
                if reason:
                    self.record(f"{group}[{index}]", reason, signal.model_dump(), "quarantine_signal")
                else:
                    kept.append(signal)
            setattr(self.bundle, group, kept)

    def review_interpretations(self):
        for index, metric in enumerate(self.bundle.metrics):
            if metric.unit in PRICE_UNITS and INFERENCE.search(metric.interpretation) and not INFERENCE.search(self.text(metric)):
                self.record(f"metrics[{index}].interpretation", "报价不能单独证明成本、利润或替代经济性",
                            {"interpretation": metric.interpretation, "impact_channels": metric.impact_channels}, "clear_interpretation")
                metric.interpretation = "来源报价观察；经济含义待核查。"
                metric.impact_channels = []
        risk_cues = {"technology": r"成本|降本|技术风险|失效|故障|缺陷|cost|failure",
                     "competition": r"竞争|毛利|盈利|利润|competition|margin",
                     "legal": r"诉讼|违法|违规|处罚|合规|争议|lawsuit|illegal"}
        kept = []
        for index, risk in enumerate(self.bundle.risks):
            cue = risk_cues.get(risk.risk_type)
            if not self.text(risk) or (cue and not re.search(cue, self.text(risk), re.I)):
                self.record(f"risks[{index}]", "风险判断超出所引正文的必要依据",
                            risk.model_dump(), "quarantine_risk")
            else:
                kept.append(risk)
        self.bundle.risks = kept
        combined = "\n".join(self.body.values())
        brief = self.bundle.brief
        if INFERENCE.search(brief.why_it_matters) and not INFERENCE.search(combined):
            self.record("brief.why_it_matters", "分析含义需要独立证据", brief.why_it_matters, "replace_analysis")
            brief.why_it_matters = "当前材料提供事件与数值线索；经济影响待核查。"
        absence = re.compile(r"[^；;。]*(?:均未|尚未|未就)[^；;。]*(?:官方|公司层面)[^；;。]*确认[^；;。]*")
        replacement = "当前提供材料未包含相关公司确认，是否另有披露尚未核查"
        old = brief.uncertainty
        if absence.search(old):
            self.record("brief.uncertainty", "将外部不存在的断言收窄为材料范围", old, "scope_statement")
            brief.uncertainty = absence.sub(replacement, old)
        for gap in list(self.bundle.coverage_gaps):
            if gap.gap_type == "missing_customer_confirmation" and absence.search(gap.description):
                old = gap.description
                gap.description = absence.sub(replacement, old)
                self.record("coverage_gaps.missing_customer_confirmation", "仅能说明提供材料中缺少确认", old, "scope_statement")

    def source_price_checks(self):
        for pid, text in self.body.items():
            amounts = list(re.finditer(rf"中标价(?:为)?\s*({NUMBER})\s*万元", text))
            prices = list(re.finditer(rf"合单价\s*({NUMBER})\s*元/Wh", text))
            pairs = list(re.finditer(rf"({NUMBER})\s*MW\s*/\s*({NUMBER})\s*MWh", text))
            if len(amounts) != 1 or len(prices) != 1 or not pairs:
                continue
            cap = sum((decimal(p.group(2)) for p in pairs), Decimal(0))
            if cap <= 0 or (len(pairs) > 1 and additive_quantity(text, cap, "MWh") is None):
                continue
            total, quoted = decimal(amounts[0].group(1)) * 10000, decimal(prices[0].group(1))
            computed = total / (cap * 1000000)
            half_quantum = Decimal(1).scaleb(quoted.as_tuple().exponent) / 2
            if abs(computed - quoted) <= half_quantum:
                continue
            detail = {"passage_id": pid, "amount_yuan": str(total), "capacity_MWh": str(cap),
                      "quoted_price_yuan_per_Wh": str(quoted), "computed_price_yuan_per_Wh": str(computed),
                      "condition": "若金额、容量与单价对应同一供货范围，则数值不相容；范围尚未确认"}
            # Stable gap also makes repeated validation idempotent.
            description = "来源报价口径待核查：" + json.dumps(detail, ensure_ascii=False, sort_keys=True)
            if description not in self.descriptions:
                self.bundle.coverage_gaps.append(CoverageGap(gap_type="source_price_basis_mismatch",
                                                            description=description, priority="high"))
                self.descriptions.add(description)
            for metric in self.bundle.metrics:
                if metric.evidence_locator.passage_id == pid and metric.unit in PRICE_UNITS:
                    metric.scope["source_price_basis_review"] = detail
            for group in ("facts", "events"):
                for item in getattr(self.bundle, group):
                    if item.evidence_locator.passage_id == pid:
                        item.metrics["source_price_basis_status"] = "pending_review"

    def apply(self):
        self.check_money()
        self.check_fields()
        self.quarantine_signals()
        from mic.evidence_gate import apply_gate
        apply_gate(self)
        self.review_interpretations()
        self.source_price_checks()
        return self.items
