"""Conservative, necessary-evidence checks for extracted relation records.

This is not an entailment classifier. Non-title evidence and literal entity
mentions are necessary, not sufficient, for a relation to be true. Aliases and
cross-passage coreference are deliberately left for review. For competitor_of,
co-mention alone is rejected; only a small set of direct affirmative statements
passes this additional filter. Passing never upgrades source confirmation.

Rejected candidates leave the structured relation list. Their original data
remains in the raw model output; warnings and coverage gaps record why.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
import unicodedata

from mic.schemas import BundleExtraction, CoverageGap, Passage


RELATION_TYPES = frozenset({
    "customer_of", "supplier_of", "competitor_of", "partner_of",
    "distributor_of", "contractor_of", "project_owner_of", "regulator_of",
    "investor_of", "subsidiary_of", "parent_of", "project_participant_of",
    "product_of", "facility_of", "brand_of",
})

REASONS = {
    "missing_body_evidence": "缺少有效的非标题正文引用",
    "duplicate_passage_id": "引用段落编号重复，无法唯一定位证据",
    "title_only_evidence": "仅引用标题，不能据此确认关系",
    "subject_not_literal": "引用正文未出现完整主体名称，别名或跨段指代待核查",
    "object_not_literal": "引用正文未出现完整客体名称，实体身份或项目业主待核查",
    "unknown_relation_type": "关系类型不在支持范围内",
    "competitor_statement_unverified": "未找到规则可识别的明确肯定竞争关系陈述；共同出现或分别中标不足以确认竞争关系",
}


def _norm(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _title(passage: Passage) -> bool:
    pid, section = _norm(passage.passage_id), _norm(passage.section)
    return bool(re.fullmatch(r"(?:title|headline)(?:[_-]?\d+)?", pid)
                or "标题" in section or "title" in section or "headline" in section)


def _explicit_competitors(text: str, subject: str, obj: str) -> bool:
    """Accept only standalone, pair-specific Chinese assertions.

    Unrecognized phrasing is pending review, not declared false. Do not look
    for a loose keyword elsewhere in the paragraph or borrow another pair's
    predicate. Negative/conditional/quoted statements are not accepted here.
    """
    text = _norm(text)
    caution = ("不", "未", "无", "否认", "传闻", "可能", "或许", "据称",
               "如果", "假如", "是否", "疑似", "预计", "将", "拟", "曾",
               "过去", "此前", "原先", "声称", "说法", "澄清", "辟谣",
               "?", "？", "“", "”", '"', "‘", "’", "「", "」")
    if any(marker in text for marker in caution):
        return False
    clauses = [c for c in re.split(r"[。.!！;；\n]", text) if c]
    for a, b in ((subject, obj), (obj, subject)):
        a, b = re.escape(_norm(a)), re.escape(_norm(b))
        patterns = (
            rf"{a}(?:与|和){b}(?:互为|是)(?:直接)?竞争对手",
            rf"{a}(?:与|和){b}存在(?:直接)?竞争关系",
            rf"{a}(?:是|为){b}的(?:直接)?竞争对手",
        )
        if any(re.fullmatch(pattern, clause) for pattern in patterns for clause in clauses):
            return True
    return False


def quarantine_relations(bundle: BundleExtraction, passages: list[Passage],
                         warnings: list[str]) -> list[dict]:
    counts = Counter(p.passage_id for p in passages)
    by_id = {p.passage_id: p for p in passages}
    kept, reviews = [], []
    gap_descriptions = {g.description for g in bundle.coverage_gaps}
    for index, relation in enumerate(bundle.relations):
        pid = relation.evidence_locator.passage_id
        passage = by_id.get(pid)
        subject, obj = relation.subject_entity.name, relation.object_entity.name
        reasons = []
        if relation.relation_type not in RELATION_TYPES:
            reasons.append("unknown_relation_type")
        if counts.get(pid, 0) > 1:
            reasons.append("duplicate_passage_id")
        elif passage is None or not passage.text.strip():
            reasons.append("missing_body_evidence")
        elif _title(passage):
            reasons.append("title_only_evidence")
        else:
            text = _norm(passage.text)
            if not _norm(subject) or _norm(subject) not in text:
                reasons.append("subject_not_literal")
            if not _norm(obj) or _norm(obj) not in text:
                reasons.append("object_not_literal")
            if (relation.relation_type == "competitor_of"
                    and not _explicit_competitors(passage.text, subject, obj)):
                reasons.append("competitor_statement_unverified")
        if not reasons:
            kept.append(relation)
            continue

        candidate = relation.model_dump(mode="json")
        key = json.dumps({"candidate": candidate, "reasons": reasons},
                         ensure_ascii=False, sort_keys=True)
        review_id = "relation-review-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        reviews.append({"review_id": review_id, "input_index": index,
                        "status": "pending_evidence", "reason_codes": reasons,
                        "candidate": candidate})
        warnings.append(f"relation evidence quarantined: relations[{index}] "
                        f"review_id={review_id}; reasons={','.join(reasons)}")
        description = (f"待核查关系：{subject} — {relation.relation_type} — {obj}；"
                       f"原引用={pid or '缺失'}；"
                       + "；".join(REASONS[reason] for reason in reasons)
                       + f"。未作为结构化关系保留。review_id={review_id}")
        if description not in gap_descriptions:
            bundle.coverage_gaps.append(CoverageGap(
                gap_type="relation_evidence_unverified", description=description,
                priority="high"))
            gap_descriptions.add(description)
    bundle.relations = kept
    return reviews
