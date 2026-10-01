"""Additional necessary-evidence gates for opt-in strict review.

No inferred arithmetic candidate is promoted to a reported measurement.
Catalysts cannot currently carry an evidence locator through schema/storage;
strict review holds them until that contract exists. Raw model JSON is retained
separately by the existing pipeline.
"""
from __future__ import annotations

import json
import re

from mic.evidence_review import NUMBER, decimal, number, quantity_supported


def _record(review, path, reason, original, action, detail=None):
    """Keep review payload in a coverage gap so it survives native storage."""
    review.record(path, reason, original, action, detail)
    item = review.items[-1]
    short = f"待核查 {path}：{reason}；review_id={item['review_id']}"
    expanded = short + '；审查记录=' + json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    for gap in review.bundle.coverage_gaps:
        if gap.description == short:
            gap.description = expanded
            review.descriptions.discard(short)
            review.descriptions.add(expanded)
            break


def _difference_candidates(review, metric):
    """Bounded diagnostics, only anchored to the cited paragraph and same unit.

    A candidate does not establish comparable supply scope, entity attribution,
    economic meaning, or a time series trend. No candidate is a value certificate.
    """
    if not re.search(r'价差|单价差|差额|difference|spread', metric.metric_name, re.I):
        return []
    if not metric.unit or decimal(metric.metric_value) is None:
        return []
    pattern = re.compile(rf'(?<![\d.,+\-])({NUMBER})\s*{re.escape(metric.unit)}(?![A-Za-z/／])')
    cited = metric.evidence_locator.passage_id
    left = [(decimal(m.group(1)), m.group(0)) for m in pattern.finditer(review.body.get(cited, ''))][:8]
    candidates = []
    for pid, text in review.body.items():
        if pid == cited:
            continue
        right = [(decimal(m.group(1)), m.group(0)) for m in pattern.finditer(text)][:8]
        for a, aq in left:
            for b, bq in right:
                if a - b == decimal(metric.metric_value):
                    candidates.append({'operator': '-', 'operands': [
                        {'passage_id': cited, 'value': str(a), 'quote': aq},
                        {'passage_id': pid, 'value': str(b), 'quote': bq}],
                        'result': number(a-b), 'unit': metric.unit,
                        'status': 'arithmetic_candidate_only',
                        'limitation': '数字可相减；跨段引用、主体产品归属与供货口径可比性未验证'})
                if len(candidates) == 4:
                    return candidates
    return candidates


def apply_gate(review):
    kept = []
    for index, metric in enumerate(review.bundle.metrics):
        if metric.metric_value is None or quantity_supported(review.text(metric), metric.metric_value, metric.unit):
            kept.append(metric)
            continue
        _record(review, f'metrics[{index}]', '数值与单位缺少所引正文支持，跨段推导未核实',
                metric.model_dump(mode='json'), 'quarantine_metric',
                {'arithmetic_candidates': _difference_candidates(review, metric)})
    review.bundle.metrics = kept

    kept = []
    for index, risk in enumerate(review.bundle.risks):
        reason, action = None, 'quarantine_risk'
        if re.search(r'行业性|非.{1,30}特定披露|并非.{1,30}特定', risk.risk_summary):
            reason = '行业背景不能直接作为目标公司的已确认风险'
        elif (risk.risk_type == 'quality'
              and re.search(r'信息|材料|报道|来源|证据|披露', risk.risk_summary)
              and re.search(r'可靠|单一|缺少|缺乏|不完整|未见|折损', risk.risk_summary)):
            reason, action = '提供材料的质量限制，应作为研究缺口而非公司经营风险', 'research_quality_gap'
        elif not review.text(risk):
            reason = '风险没有唯一可定位的非标题正文'
        if reason:
            _record(review, f'risks[{index}]', reason, risk.model_dump(mode='json'), action,
                    {'scope': '仅针对当前提供材料；未检索外部是否存在补充披露'})
        else:
            kept.append(risk)
    review.bundle.risks = kept

    for index, catalyst in enumerate(review.bundle.catalysts):
        _record(review, f'catalysts[{index}]', '当前催化剂结构缺少贯通入库的证据定位，尚不能核实事项与预期日期',
                catalyst.model_dump(mode='json'), 'quarantine_catalyst',
                {'scope': '待核查线索；不由已发生的中标推定未来交付、投运安排或经济影响'})
    review.bundle.catalysts = []
