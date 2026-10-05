"""Clause-level review of analytical statements across the whole bundle.

Codex review F1/F2 (2026-10-04): the per-field checks in ``evidence_review``
matched a few phrasings of unsupported economics (``MATERIALITY``), only on
metric interpretations and the brief, and the source-price limitation stopped
at metrics citing the same passage. Equivalent claims in a fact statement, an
event summary, an event ``impact`` or a brief sentence went through formal
output untouched.

This module classifies every clause of every narrative field by the kind of
analytical claim it makes and checks the *necessary* evidence for that kind in
the cited passage. It is not a semantic entailment checker: observations
(who won what, amount, capacity, quoted price) are kept; clauses asserting

* relative financial importance (order vs. company revenue/profit),
* competitive consequence (rival award read as competition impact),
* cost / margin / economics,
* comparability of quotations whose supply scope is unresolved,

are moved into pending-review slots on the owning record. Cross-passage price
comparisons keep the model's wording only when every quoted price is found in
some passage; the other side's passage is attached as ``comparison_evidence``
and the price-basis limitation travels with the record.
Raw model output is never mutated; ``EvidenceReview`` works on a deep copy.
"""
from __future__ import annotations

import re

from mic.schemas import EventImpact

NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
PRICE_UNIT = r"元/(?:Wh|kWh|MWh|吨|公斤|千克|件)"
# Full-width comma only: an ASCII comma may be a thousands separator.
CLAUSE_SPLIT = re.compile(r"(?<=[。；;，])")

# Company-level financial bases. An order amount alone never establishes its
# share of these; a base figure in the passage (or the literal sentence) does.
FINANCIAL_BASE = re.compile(r"收入|营收|营业额|利润|净利|业绩|出货|总盘|营业规模")
MAGNITUDE = re.compile(r"有限|很小|较小|不大|较低|可忽略|微小|微不足道|重大|显著|较大|可观|重要|关键|体量")
RELATIVE = re.compile(r"贡献|占比|相对|占|比重|影响")
BASE_FIGURE = re.compile(rf"(?:收入|营收|营业额|利润|净利|业绩|出货)[^。；;]{{0,24}}{NUMBER}\s*(?:亿|万)?(?:元|%|％|GWh|MWh)")
# "体量较小 / 规模虽小 / 金额不大": a size judgement about an order with no stated base
# (batch v3 leak). Kept only when the clause itself states the base (占…%, 倍, 分之).
SIZE_WORD = re.compile(r"体量|规模|金额|订单|量级|数额|单笔")
SIZE_ADJ = re.compile(r"很小|较小|虽小|偏小|不小|不大|有限|微小|微不足道|较大|偏大|可观|庞大|巨大")
IN_CLAUSE_BASE = re.compile(r"占|%|％|分之|倍|大于|小于|高于|低于")

COMPETITION_CLAIM = re.compile(r"竞争|同台|替代|取代|丢标|落标|挤占|份额|压力")
COMPETITION_CUE = re.compile(r"竞争|丢标|落标|替代|取代|份额|未中标|流标|挤占|竞标|竞购|同台")

ECONOMICS_CLAIM = re.compile(r"成本|毛利|盈利|利润率|经济性|溢价|替代路径|降本|cost|margin|profit", re.I)
ECONOMICS_CUE = re.compile(r"成本|毛利|盈利|利润率|经济性|溢价|降本|cost|margin|profit", re.I)

PRICE_WORD = re.compile(rf"价|{PRICE_UNIT}|元/")
PRICE_ORDERING = re.compile(r"高于|低于|持平|倍|差距|相差|高出|低出|翻番|一半|更低|更高|较低|较高|偏低|偏高|相比|便宜|昂贵|之上|之下")
PRICE_COMPARABILITY = re.compile(r"可比|参照|基准|对照|对比|比较|benchmark", re.I)

# Hedged or already-held wording is a statement of what is unknown, not a claim.
HEDGED = re.compile(r"待核查|待核实|尚未核实|未核实|未知|不明|未披露|尚不|无法确定|有待|暂不用于|不作为|不能用作|不构成")

LEADING_CONJUNCTION = re.compile(r"^(?:但是|但|而且|而|且|却|并且|并|也|则|不过|然而|同时)")

HOLD_MATERIALITY = "订单对公司收入的贡献尚未核实。"
HOLD_COMPETITION = "对目标公司竞争影响的判断缺少依据，待核查。"
HOLD_ECONOMICS = "成本或盈利含义待核查。"
HOLD_PRICE_BASIS = "同项目两个标段报价的供货范围与口径尚未核实，不作为可比基准。"
HOLD_PRICE_COMPARISON = "与其他报价的比较缺少另一侧的数值依据，待核查。"


def clauses(text):
    return [part for part in CLAUSE_SPLIT.split(text or "") if part.strip()]


def _bare(clause):
    return clause.strip().rstrip("。；;， ")


def prices_with_units(text):
    return {(m.group(1), m.group(2)) for m in re.finditer(rf"(?<![\d.])({NUMBER})\s*({PRICE_UNIT})", text or "")}


def prices_in(text):
    return {value for value, _unit in prices_with_units(text)}


def price_unit_in(text):
    units = {unit for _value, unit in prices_with_units(text)}
    return units.pop() if len(units) == 1 else None


def classify(clause, passage, *, price_basis_pending, combined):
    """Return (kind, replacement) when the clause needs evidence it lacks, else None."""
    bare = _bare(clause)
    if not bare or HEDGED.search(bare) or bare in passage:
        return None
    if FINANCIAL_BASE.search(bare) and (MAGNITUDE.search(bare) or RELATIVE.search(bare)) \
            and not BASE_FIGURE.search(passage):
        return "materiality", HOLD_MATERIALITY
    if SIZE_WORD.search(bare) and SIZE_ADJ.search(bare) and not IN_CLAUSE_BASE.search(bare) \
            and not BASE_FIGURE.search(passage):
        return "materiality", HOLD_MATERIALITY
    if COMPETITION_CLAIM.search(bare) and not COMPETITION_CUE.search(passage):
        return "competition", HOLD_COMPETITION
    if ECONOMICS_CLAIM.search(bare) and not ECONOMICS_CUE.search(passage):
        return "economics", HOLD_ECONOMICS
    if PRICE_WORD.search(bare) and PRICE_COMPARABILITY.search(bare) and price_basis_pending \
            and not PRICE_COMPARABILITY.search(combined):
        return "price_basis", HOLD_PRICE_BASIS
    return None


class StatementReview:
    """Runs inside ``EvidenceReview.apply`` after the per-field checks."""

    def __init__(self, review):
        self.review = review
        self.bundle = review.bundle
        self.body = review.body
        self.combined = "\n".join(self.body.values())
        self.price_basis_pids = set(getattr(review, "price_basis_pids", set()))
        self.target_names = list(getattr(review, "target_names", []) or [])

    # --- helpers -----------------------------------------------------------

    def _hold(self, path, kind, original, detail):
        reasons = {"materiality": "收入或利润贡献缺少基数及可核实依据",
                   "competition": "竞争影响判断缺少中标以外的竞争依据",
                   "economics": "成本或盈利含义超出所引正文",
                   "price_basis": "报价口径存疑，不能用作可比基准"}
        self.review.record(path, reasons[kind], original, f"hold_{kind}", detail)

    def _rewrite(self, text, passage, path):
        """Drop unsupported clauses; return (new_text, held) with held=[{clause, kind}]."""
        kept, held = [], []
        dropped_previous = False
        for clause in clauses(text):
            found = classify(clause, passage, price_basis_pending=bool(self.price_basis_pids),
                             combined=self.combined)
            if found:
                held.append({"clause": _bare(clause), "kind": found[0]})
                dropped_previous = True
            else:
                if dropped_previous:
                    # "体量虽小，但为…" -> "为…": the contrast word belonged to the dropped clause.
                    clause = LEADING_CONJUNCTION.sub("", clause.lstrip())
                kept.append(clause)
                dropped_previous = False
        if not held:
            return text, held
        body = "".join(kept).rstrip("。；;， ")
        tails = []
        for item in held:
            tail = {"materiality": HOLD_MATERIALITY, "competition": HOLD_COMPETITION,
                    "economics": HOLD_ECONOMICS, "price_basis": HOLD_PRICE_BASIS}[item["kind"]]
            if tail not in tails:
                tails.append(tail)
        for item in held:
            self._hold(path, item["kind"], text, {"clause": item["clause"]})
        return (body + "；" if body else "") + "".join(tails), held

    def _price_comparison(self, item, path, statement):
        """Both sides of a quoted-price comparison must be evidenced; attach limitation.

        Codex review R3: "0.518元/Wh，显著低于钠电二标段" names the other side without
        its number. An elided comparison has the same evidence requirement as an
        explicit one — the other quotation is resolved from the document (unique
        other price in the same unit) and attached, or the comparison clause is
        moved to pending review while the quoted observation stays.
        """
        if not PRICE_ORDERING.search(statement) and not PRICE_COMPARABILITY.search(statement):
            return
        quoted = prices_in(statement)
        if not quoted:
            return
        own = item.evidence_locator.passage_id
        evidence, missing, touched = [], [], set()
        for value in sorted(quoted):
            pids = [pid for pid, text in self.body.items() if value in prices_in(text)]
            if not pids:
                missing.append(value)
                continue
            touched.update(pids)
            if own not in pids:
                evidence.append({"value": value, "passage_id": pids[0]})
        if missing:
            item.metrics["comparison_review"] = {"status": "pending_review", "original": statement,
                                                 "unsupported_prices": missing}
            self._hold(path, "economics", statement, {"unsupported_prices": missing})
            self._set_statement(item, HOLD_ECONOMICS)
            return
        if len(quoted) == 1 and PRICE_ORDERING.search(statement):
            # Elided other side: resolve it from the document or hold the comparison clause.
            unit = price_unit_in(statement)
            others = {}
            for pid, text in self.body.items():
                for value, text_unit in prices_with_units(text):
                    if value not in quoted and (unit is None or text_unit == unit):
                        others.setdefault(value, pid)
            if len(others) == 1:
                (value, pid), = others.items()
                evidence.append({"value": value, "passage_id": pid, "resolved_from": "elided_reference"})
                touched.add(pid)
            else:
                kept, held = [], []
                for clause in clauses(statement):
                    (held if PRICE_ORDERING.search(clause) and not prices_in(clause) else kept).append(clause)
                if not held:   # ordering word sits in the clause with the price: hold the wording only
                    kept, held = [], [statement]
                item.metrics["comparison_review"] = {
                    "status": "pending_review", "original": statement,
                    "reason": "other side of the comparison is not quoted and cannot be resolved uniquely",
                    "held_clauses": [_bare(c) for c in held],
                    "candidate_prices": sorted(others)}
                self._hold(path, "price_basis" if self.price_basis_pids else "economics", statement,
                           {"held_clauses": [_bare(c) for c in held], "candidates": sorted(others)})
                body = "".join(kept).rstrip("。；;， ")
                quote = next(iter(quoted)) + (unit or "")
                self._set_statement(item, (body + "；" if body else f"来源报价{quote}；") + HOLD_PRICE_COMPARISON)
                return
        if evidence:
            item.metrics["comparison_evidence"] = evidence
        if self.price_basis_pids:
            # The quotations being compared come from passages whose supply
            # scope is unresolved; the ordering may stand, comparability may not.
            item.metrics["source_price_basis_status"] = "pending_review"
            item.metrics["usable_as_price_benchmark"] = False
            item.metrics["price_basis_passages"] = sorted(touched & self.price_basis_pids)

    @staticmethod
    def _statement(item):
        return item.fact_statement if hasattr(item, "fact_statement") else item.summary

    @staticmethod
    def _set_statement(item, value):
        if hasattr(item, "fact_statement"):
            item.fact_statement = value
        else:
            item.summary = value

    # --- passes ------------------------------------------------------------

    def review_facts_and_events(self):
        for group in ("facts", "events"):
            for index, item in enumerate(getattr(self.bundle, group)):
                passage = self.review.text(item)
                statement = self._statement(item)
                new_text, held = self._rewrite(statement, passage, f"{group}[{index}].statement")
                if held:
                    item.metrics["statement_review"] = {"status": "pending_review", "original": statement,
                                                        "held_clauses": held}
                    self._set_statement(item, new_text)
                self._price_comparison(item, f"{group}[{index}].statement", self._statement(item))
        for index, event in enumerate(self.bundle.events):
            self._review_impact(event, index)

    def _subject_is_target(self, event):
        """True unless the event is about another named party and never names the target."""
        names = [n for n in self.target_names if n]
        if not names:
            return True
        subject = (event.entities or {}).get("subject") or ""
        text = f"{subject} {event.summary or ''}"
        return (not subject) or any(n in text for n in names)

    def _review_impact(self, event, index):
        passage = self.review.text(event)
        impact = event.impact
        reasons = []
        if "competition" in impact.channels and not COMPETITION_CUE.search(passage):
            reasons.append("competition")
        if set(impact.channels) & {"cost", "margin"} and not ECONOMICS_CUE.search(passage):
            reasons.append("economics")
        # A rival's award (batch v3: 远景能源 lot 1, impact neutral/revenue) carries no
        # evidenced channel to the target company; the impact is a candidate, not a finding.
        if (impact.channels or impact.direction not in ("", "unclear")) and not self._subject_is_target(event):
            reasons.append("competition")
        if not reasons:
            return
        original = impact.model_dump()
        event.metrics["impact_review"] = {"status": "pending_review", "impact": original,
                                          "reasons": sorted(set(reasons))}
        for kind in sorted(set(reasons)):
            self._hold(f"events[{index}].impact", kind, original, {"channels": list(impact.channels)})
        event.impact = EventImpact()

    def review_metrics(self):
        for index, metric in enumerate(self.bundle.metrics):
            passage = self.review.text(metric)
            new_text, held = self._rewrite(metric.interpretation, passage, f"metrics[{index}].interpretation")
            if not held:
                continue
            kinds = {h["kind"] for h in held}
            original = {"interpretation": metric.interpretation, "impact_channels": list(metric.impact_channels)}
            slot = "materiality_review" if kinds == {"materiality"} else "economic_interpretation_review"
            metric.scope.setdefault(slot, {"status": "pending_review", **original})
            amount_metric = (metric.unit or "") in ("元", "万元", "亿元", "CNY")
            metric.interpretation = ("来源订单金额观察；收入贡献及确认节奏待核查。"
                                     if kinds == {"materiality"} and amount_metric else new_text)
            metric.impact_channels = []

    def review_brief(self):
        brief = self.bundle.brief
        for field in ("why_it_matters", "uncertainty", "one_sentence", "what_happened"):
            value = getattr(brief, field)
            new_text, held = self._rewrite(value, self.combined, f"brief.{field}")
            if held:
                setattr(brief, field, new_text)

    def apply(self):
        self.review_facts_and_events()
        self.review_metrics()
        self.review_brief()
