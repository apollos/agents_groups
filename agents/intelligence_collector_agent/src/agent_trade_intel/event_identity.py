"""Business-event identity for structured_events (Codex reviews F3 2026-10-04, R1/R2 2026-10-05).

The persistence key used to be (target, event_type, event_date, hash(summary)).
Two media copies of the same award — reworded, or labelled ``tender`` by one
model call and ``major_order`` by the next — therefore became two "new" events.

A business event is identified by *who did what, in which matter, at what size*:

* ``subject``   ``entities.subject`` resolved against the known names of the
                collection target (profile canonical name + aliases: "宁德时代新能源
                科技股份有限公司" ≡ "宁德时代"); any other company is normalised
                only (suffix removed) — sharing a target_id never makes two
                subjects the same company; a project-like subject
                ("河北任丘智弘独立储能试点项目") is reduced to its project key
* ``family``    the action family of ``event_type`` (award, capacity, price, ...)
* ``scope``     the matter: project key and lot ("二标段"), taken from entities /
                summary. A project name is ``<distinctive head><type descriptors>项目``
                ("河北任丘智弘" + "磷酸铁锂+钠电独立储能试点" + "项目"); the key is
                the head (province prefix dropped), so descriptor variants and
                truncations ("储能项目" / "储能试点项目" / "钠电独立储能试点项目")
                of one project agree while "甲地示例" and "乙地示例" stay apart.
                The raw names are kept next to the key (``business_identity``)
* quantities    energy in MWh and amount in 万元 — only from fields whose unit is
                known (MIC canonical ``energy_mwh`` / ``amount_unit``, or an
                explicit unit); a bare number is *unknown*, never "MWh"

Two source rows describe the same business event when subject, family and
scope match, every quantity known on both sides is equal, known event dates
agree and the publications fall in the same news cycle. The second review
showed both failure modes of a quantity-only rule: the same project notice
split by a "独立" in the subject and a 100-vs-400 unit mix-up, and two
different projects of identical size merged. Scope is therefore mandatory for
award-family events: a row whose project cannot be determined is ``unresolved``
and is never merged; identical quantities alone never prove identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Iterable

ACTION_FAMILY = {
    "major_order": "award", "tender": "award", "customer_order": "award", "order": "award",
    "contract": "award", "bid": "award", "contract_award": "award", "tender_result": "award",
    "capacity_change": "capacity", "capacity_commissioning": "capacity",
    "price_change": "price",
    "policy_change": "policy",
    "customer_change": "relationship", "supplier_change": "relationship",
    "financing": "financing", "mna": "mna", "product_launch": "product",
    "management_change": "management", "earnings_change": "earnings", "risk_event": "risk",
}
SCOPE_REQUIRED_FAMILIES = {"award"}
SAME_NEWS_CYCLE_DAYS = 45
COMPANY_SUFFIX = re.compile(r"(股份有限公司|有限责任公司|有限公司|集团公司|集团|公司|股份|\(.*?\)|（.*?）)")
NUMBER = r"\d+(?:\.\d+)?"
ENERGY_FACTOR = {"kwh": 0.001, "mwh": 1.0, "gwh": 1000.0}
QUANTITY_TOKEN = re.compile(rf"{NUMBER}\s*(?:GWh|MWh|kWh|GW|MW|kW)(?:\s*/\s*{NUMBER}\s*(?:GWh|MWh|kWh|GW|MW|kW))*", re.I)

# Project naming: "<distinctive head><type descriptors>项目". Descriptors say what kind of
# project it is, not which one: the identity is the head, i.e. the text before the first
# descriptor ("河北任丘智弘" in "河北任丘智弘磷酸铁锂+钠电独立储能试点项目"). Cutting at the
# first descriptor (rather than deleting descriptor words anywhere) keeps the head intact
# and does not depend on the descriptor list being complete ("钠电" vs "钠电池").
PROJECT_WORD = re.compile(r"(?:项目|电站|基地|园区)")
PROJECT_PATTERN = re.compile(rf"([^，。；,;：:（）()\s]{{2,60}}?(?:项目|电站|基地|园区))")
GENERIC_DESCRIPTORS = re.compile(
    r"新型技术路线|独立|共享|集中式|分布式|电网侧|用户侧|电源侧|源网荷储|风光储|光储|储能|试点|示范|"
    r"磷酸铁锂|钠离子|钠电|锂电|电池|设备采购|采购|中标结果|公示|\+|＋"
)
# Verbs that precede the project name in a summary ("…中标河北任丘智弘…项目"): the head
# starts after the last of them.
VERBS_BEFORE_PROJECT = re.compile(r"中标|预中标|承建|承接|签约|签订|获得|竞得|投资|建设|公示|入围|参与|承揽|拟")
PROJECT_TAIL = re.compile(r"(?:项目|电站|基地|园区)$")
# Media copies name the same project with or without its province ("河北任丘智弘" / "任丘智弘").
PROVINCE_PREFIX = re.compile(
    r"^(?:北京|天津|上海|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|海南|"
    r"四川|贵州|云南|陕西|甘肃|青海|台湾|内蒙古|广西|西藏|宁夏|新疆|香港|澳门)(?:省|市|自治区)?")
LEADING_VERBS = re.compile(r"^(?:成功|再次|拟|已|在|于)?(?:中标|承建|承接|签约|签订|获得|获|参与|承揽|入围|预中标|竞得|投资|建设|公示)+")
LOT = re.compile(r"((?:[一二三四五六七八九十\d]+)标段|标段[一二三四五六七八九十\d]+)")


def _norm(text: Any) -> str:
    value = re.sub(r"\s+", "", str(text or "")).lower()
    value = COMPANY_SUFFIX.sub("", value)
    return re.sub(r"[·•・,，。；;:：/／\-—_]", "", value)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(rf"(?<![\d.])({NUMBER})", str(value).replace(",", ""))
    return float(match.group(1)) if match else None


# --- scope -------------------------------------------------------------------

def project_name(text: Any) -> str:
    """The project name as written in ``text`` (capacity tokens removed); '' when none."""
    raw = re.sub(r"\s+", "", str(text or ""))
    if not raw or not PROJECT_WORD.search(raw):
        return ""
    raw = QUANTITY_TOKEN.sub("", raw)
    raw = re.sub(r"\(.*?\)|（.*?）", "", raw)
    match = PROJECT_PATTERN.search(raw)
    if not match:
        return ""
    name = match.group(1)
    verbs = [v.end() for v in VERBS_BEFORE_PROJECT.finditer(name) if v.end() < len(name) - 2]
    return name[verbs[-1]:] if verbs else name


def project_key(text: Any) -> str:
    """Distinctive head of a project name; '' when the text names no identifiable project."""
    name = PROJECT_TAIL.sub("", project_name(text))
    if not name:
        return ""
    descriptor = GENERIC_DESCRIPTORS.search(name)
    head = name[:descriptor.start()] if descriptor else name
    stripped = PROVINCE_PREFIX.sub("", head)
    if len(stripped) >= 2:
        head = stripped
    return _norm(head) if len(head) >= 2 else ""


class EntityAliases:
    """Known names of one entity (the collection target: profile canonical name + aliases).

    ``resolve`` maps any of those spellings to one key; every other subject is only
    normalised. No prefix / fuzzy matching: "远景能源" in a 宁德时代 task stays "远景能源".
    """

    def __init__(self, names: Iterable[str], *, key: str | None = None):
        cleaned = [str(n).strip() for n in names if n and str(n).strip()]
        self.names = list(dict.fromkeys(cleaned))
        self._norms = {_norm(n) for n in self.names if _norm(n)}
        # Prefer the short name the task uses (keeps keys of earlier saves stable), else the first.
        self.key = _norm(key) if key and _norm(key) in self._norms else (_norm(self.names[0]) if self.names else "")

    def resolve(self, subject: Any) -> tuple[str, bool]:
        """(subject key, resolved against known names?)"""
        norm = _norm(subject)
        if norm and norm in self._norms:
            return self.key, True
        return norm, False


def lot_key(text: Any) -> str:
    match = LOT.search(str(text or ""))
    if not match:
        return ""
    lot = match.group(1).replace("标段", "")
    return f"标段{lot}"


def _strip_subject_and_verbs(summary: str, subject: str) -> str:
    text = re.sub(r"\s+", "", summary or "")
    if subject:
        text = text.replace(re.sub(r"\s+", "", subject), "", 1)
    return LEADING_VERBS.sub("", text)


def scope_of(event: dict[str, Any]) -> dict[str, str]:
    """Project key and lot of the matter an event is about."""
    entities = event.get("entities") or {}
    subject = str(entities.get("subject") or "")
    summary = str(event.get("summary") or event.get("summary_cn") or "")
    candidates = [entities.get("object"), entities.get("counterparty_candidate"), entities.get("counterparty"),
                  _strip_subject_and_verbs(summary, subject), subject]
    project = project_raw = ""
    for candidate in candidates:
        project = project_key(candidate)
        if project:
            project_raw = project_name(candidate)
            break
    lot = ""
    for candidate in (entities.get("object"), entities.get("product"), subject):
        lot = lot_key(candidate)
        if lot:
            break
    if not lot:
        lot = _lot_from_summary(summary, subject)
    return {"project": project, "lot": lot, "project_raw": project_raw}


def _lot_from_summary(summary: str, subject: str) -> str:
    """The lot of the subject's own clause; a unique lot in the summary; else ''."""
    clauses = [c for c in re.split(r"[，。；,;：:]", summary or "") if c.strip()]
    subject_norm = _norm(subject)
    if subject_norm:
        for clause in clauses:
            if subject_norm in _norm(clause) and lot_key(clause):
                return lot_key(clause)
    lots = {lot_key(c) for c in clauses if lot_key(c)}
    return lots.pop() if len(lots) == 1 else ""


# --- quantities ----------------------------------------------------------------

def capacity_mwh(metrics: dict[str, Any]) -> float | None:
    """Energy in MWh from fields whose unit is known; None when no unit is established."""
    canonical = metrics.get("energy_mwh")
    if isinstance(canonical, dict):
        canonical = canonical.get("canonical", canonical.get("value"))
    if isinstance(canonical, (int, float)) and not isinstance(canonical, bool):
        return float(canonical)
    for key in ("capacity", "volume"):
        raw = metrics.get(key)
        if raw is None:
            continue
        unit = str(metrics.get(f"{key}_unit") or "").lower()
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if unit in ENERGY_FACTOR:
                return float(raw) * ENERGY_FACTOR[unit]
            continue   # bare number or a power unit: not an energy quantity
        matches = re.findall(rf"({NUMBER})\s*(GWh|MWh|kWh)(?![A-Za-z])", str(raw), re.I)
        if len(matches) == 1:   # several energy tokens ("120MWh+240MWh") are ambiguous → unknown
            return float(matches[0][0]) * ENERGY_FACTOR[matches[0][1].lower()]
    return None


def amount_wan(metrics: dict[str, Any]) -> float | None:
    """Amount in 万元 from the reviewed metrics block; None when the unit is not certain."""
    if metrics.get("amount") is not None and metrics.get("amount_unit") == "元":
        return round(float(metrics["amount"]) / 10000, 3)
    if metrics.get("amount_raw") is not None and metrics.get("amount_raw_unit") == "万元":
        return round(float(metrics["amount_raw"]), 3)
    candidate = metrics.get("amount_candidate", metrics.get("amount"))
    value = _num(candidate)
    if value is None:
        return None
    unit_text = " ".join(str(metrics.get(k) or "") for k in ("currency", "unit", "amount_unit", "amount_raw_unit"))
    if "亿元" in unit_text:
        return round(value * 10000, 3)
    if "万元" in unit_text:
        return round(value, 3)
    return None


# --- signature ---------------------------------------------------------------

@dataclass(frozen=True)
class EventSignature:
    subject: str
    family: str
    project: str
    lot: str
    capacity_mwh: float | None
    amount_wan: float | None
    product: str
    event_date: str | None
    published_at: str | None
    # Source context the key was derived from (kept with the row, not part of the key).
    subject_raw: str = ""
    subject_resolved: bool = False
    project_raw: str = ""

    @property
    def business_key(self) -> str:
        return f"{self.subject}|{self.family}|{self.project}|{self.lot}"

    def describe(self) -> dict[str, Any]:
        """How this row was identified — stored as ``business_identity`` on the persisted payload."""
        return {
            "business_key": self.business_key,
            "subject": {"raw": self.subject_raw, "key": self.subject,
                        "resolution": "target_alias" if self.subject_resolved else "normalized"},
            "project": {"raw": self.project_raw, "key": self.project},
            "lot": self.lot,
            "energy_mwh": self.capacity_mwh,
            "amount_wan": self.amount_wan,
            "event_date": self.event_date[:10] if self.event_date else None,
        }

    def compatible_with(self, other: "EventSignature") -> bool:
        if self.business_key != other.business_key:
            return False
        for mine, theirs in ((self.capacity_mwh, other.capacity_mwh), (self.amount_wan, other.amount_wan)):
            if mine is not None and theirs is not None:
                if abs(mine - theirs) > 0.005 * max(abs(mine), abs(theirs), 1.0):
                    return False
        if not self.project:
            # No matter scope (non-award families): fall back to the quantity / product rule.
            shared = sum(1 for mine, theirs in ((self.capacity_mwh, other.capacity_mwh),
                                                (self.amount_wan, other.amount_wan))
                         if mine is not None and theirs is not None)
            if shared == 0 and not (self.product and self.product == other.product):
                return False
        if self.event_date and other.event_date and self.event_date[:10] != other.event_date[:10]:
            return False
        if self.published_at and other.published_at:
            gap = _days_between(self.published_at, other.published_at)
            if gap is not None and gap > SAME_NEWS_CYCLE_DAYS:
                return False
        return True


def _days_between(a: str, b: str) -> int | None:
    try:
        da, db = datetime.fromisoformat(a[:19]), datetime.fromisoformat(b[:19])
    except ValueError:
        return None
    return abs((da - db).days)


def signature(event: dict[str, Any], *, published_at: str | None,
              aliases: EntityAliases | None = None) -> EventSignature | None:
    entities = event.get("entities") or {}
    metrics = event.get("metrics") or {}
    raw_subject = entities.get("subject")
    family = ACTION_FAMILY.get(str(event.get("event_type") or ""), str(event.get("event_type") or ""))
    if not raw_subject or not family or family in ("other", "unknown"):
        return None
    scope = scope_of(event)
    # A project named as the subject ("…储能试点项目" announcing its award result);
    # otherwise a company, resolved against the target's known names.
    resolved = False
    subject = project_key(raw_subject)
    if not subject:
        subject, resolved = (aliases or EntityAliases([])).resolve(raw_subject)
    if not subject:
        return None
    if family in SCOPE_REQUIRED_FAMILIES and not scope["project"]:
        return None   # unresolved: identical quantities alone never prove the same matter
    return EventSignature(
        subject=subject, family=family, project=scope["project"], lot=scope["lot"],
        capacity_mwh=capacity_mwh(metrics), amount_wan=amount_wan(metrics),
        product=_norm(entities.get("product")),
        event_date=event.get("event_date") or event.get("date"),
        published_at=published_at,
        subject_raw=str(raw_subject), subject_resolved=resolved, project_raw=scope["project_raw"],
    )
