"""Low-confidence keyword mapping from events to tracking variables (V0.8).

Two-layer labeling contract:
- The MIC model emits confirmed/pending ``tracking_variables`` per event (mapping_method
  ``mic_model``). Those may become ``accepted`` coverage.
- This module only produces ``keyword_candidate`` mappings that are always saved as
  ``pending``. Candidates never enter confirmed coverage without review, so noisy keyword
  matches cannot pollute the fact base while still bootstrapping the coverage matrix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CandidateVariable:
    variable: str
    direction: str = "unclear"
    confidence: float = 0.4
    evidence: str = ""


DEFAULT_KEYWORD_RULES: dict[str, list[str]] = {
    "orders": ["订单", "合同", "中标", "定点", "客户项目", "backlog"],
    "backlog": ["在手订单", "订单储备", "合同负债"],
    "contract_liabilities": ["合同负债", "预收款"],
    "gross_margin": ["毛利率", "毛利", "价格传导", "盈利能力"],
    "receivables": ["应收账款", "回款", "账期"],
    "inventory": ["库存", "存货", "备货"],
    "cash_conversion": ["经营现金流", "现金转换", "自由现金流", "现金流"],
    "capex": ["资本开支", "扩产", "产能建设", "CAPEX"],
    "capex_cycle": ["资本开支", "扩产", "产能建设", "CAPEX"],
    "policy_change": ["政策", "监管", "补贴", "目录", "审批"],
    "southbound_holding": ["南向持股", "港股通持股", "沪深港通持股"],
    "buyback": ["回购", "注销"],
    "ah_premium": ["AH溢价", "A/H", "H股折价"],
    "export_control": ["出口管制", "禁令", "制裁", "关税"],
    "license_out": ["license-out", "授权", "海外授权", "里程碑付款"],
    "cde_acceptance": ["CDE", "临床试验申请", "受理"],
    "nmpa_approval": ["NMPA", "获批上市", "批准"],
    "overseas_revenue": ["海外收入", "出口", "海外市场", "国际化"],
    "fx_impact": ["汇率", "汇兑", "人民币贬值", "人民币升值"],
    "dividend_policy": ["分红", "股息", "派息", "分红率"],
    "grid_tender": ["电网招标", "国网招标", "南网招标", "特高压"],
}


class CandidateVariableMapper:
    """Maps event text to candidate tracking variables via keyword rules."""

    def __init__(self, rules: dict[str, list[str]] | None = None):
        self.rules = rules or DEFAULT_KEYWORD_RULES

    def map_event(self, *, event: dict[str, Any], allowed_variables: list[str]) -> list[CandidateVariable]:
        # Candidates only make sense against a target's declared variable list; without a
        # list every keyword rule would fire and the pending queue becomes noise.
        allowed = {str(v) for v in allowed_variables if v}
        if not allowed:
            return []
        text = " ".join(
            str(x or "")
            for x in [
                event.get("summary"),
                event.get("summary_cn"),
                event.get("event_type"),
                event.get("impact"),
                event.get("impact_channels"),
            ]
        ).lower()

        out: list[CandidateVariable] = []
        for variable, keywords in self.rules.items():
            if variable not in allowed:
                continue
            hits = [kw for kw in keywords if kw.lower() in text]
            if not hits:
                continue
            out.append(
                CandidateVariable(
                    variable=variable,
                    direction=self._infer_direction(text),
                    confidence=min(0.6, 0.3 + 0.1 * len(hits)),
                    evidence=",".join(hits[:5]),
                )
            )
        return out

    @staticmethod
    def _infer_direction(text: str) -> str:
        negative = ["下降", "减少", "恶化", "下滑", "亏损", "风险", "减值", "取消"]
        positive = ["增长", "增加", "改善", "中标", "获批", "回购", "上升"]
        has_neg = any(x in text for x in negative)
        has_pos = any(x in text for x in positive)
        if has_neg and has_pos:
            return "mixed"
        if has_neg:
            return "negative"
        if has_pos:
            return "positive"
        return "unclear"
