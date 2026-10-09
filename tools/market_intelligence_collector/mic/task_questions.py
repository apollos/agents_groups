"""Task questions: a caller's concrete research question steering one collection.

``focus`` selects *query families* (generic templates such as "{company} 重大合同 公告").
That is right for monitoring, but a caller that needs one specific answer
("宁德时代 2025 年度营业收入 / 归母净利润 / 经营现金流及同比") has no family that
asks for it. ``task_profile["questions"]`` carries such questions explicitly:

.. code-block:: python

    task_profile = {
        "focus": ["financial_leading_indicator"],
        "questions": [
            {"question": "宁德时代2025年全年营业收入、归母净利润、经营活动现金流量净额及同比",
             "search_terms": ["2025年年度报告 营业收入 归母净利润 经营活动产生的现金流量净额"],
             "period": "2025年度"},
            "宁德时代 2025年 业绩快报",   # plain string: the text itself is the search phrase
        ],
    }

Semantics (no company- or topic-specific logic lives here):

* every ``search_terms`` entry becomes one query of family ``task_question`` and is
  planned *before* family templates (the caller asked for it explicitly); the
  deployment / task query budget is unchanged, so questions consume slots the
  caller would otherwise spend on templates;
* a question without ``search_terms`` uses its own text as the search phrase -
  natural language is passed to the engine as-is, nothing is "understood" here;
* question terms count as task keywords in SERP triage and passage selection, and
  the question text + period are given to the extraction model as ``task_context``
  so it reports the matching metric with explicit period / unit / scope or says the
  source does not answer it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

QUESTION_FAMILY = "task_question"
MAX_QUESTIONS = 8
MAX_SEARCH_TERMS_PER_QUESTION = 4

_SPLIT_RE = re.compile(r"[\s、，,；;：:。！？!?（）()\[\]【】\"'“”‘’/]+")
_TERM_STOPWORDS = {"公告", "新闻", "资讯", "最新", "相关", "公司", "股份", "有限公司", "the", "and", "of",
                   "a", "及", "和", "与", "或", "等", "明确", "收集", "整理", "查询"}


@dataclass
class TaskQuestion:
    question: str
    search_terms: list[str] = field(default_factory=list)
    period: str | None = None

    @classmethod
    def from_value(cls, value: Any) -> TaskQuestion | None:
        if isinstance(value, str):
            text = value.strip()
            return cls(question=text) if text else None
        if isinstance(value, dict):
            text = str(value.get("question") or "").strip()
            if not text:
                return None
            raw_terms = value.get("search_terms") or []
            if isinstance(raw_terms, str):
                raw_terms = [raw_terms]
            terms: list[str] = []
            for t in raw_terms:
                t = " ".join(str(t).split())
                if t and t not in terms:
                    terms.append(t)
            period = value.get("period")
            return cls(question=text, search_terms=terms[:MAX_SEARCH_TERMS_PER_QUESTION],
                       period=str(period).strip() if period else None)
        return None

    # -- derived views --------------------------------------------------------

    def search_phrases(self) -> list[str]:
        """Phrases sent to the search engine (explicit terms, else the question text)."""
        return list(self.search_terms) if self.search_terms else [" ".join(self.question.split())]

    def terms(self) -> list[str]:
        """Tokens used for triage / passage scoring.

        Explicit ``search_terms`` are whitespace-split and, when present, are the only
        source (the caller stated what to look for). Otherwise the question text is split
        on punctuation so that enumerations ("营业收入、归母净利润") yield one token each.
        """
        out: list[str] = []
        if self.search_terms:
            for phrase in self.search_terms:
                for tok in phrase.split():
                    _add_term(out, tok)
        else:
            for tok in _SPLIT_RE.split(self.question):
                _add_term(out, tok)
        if self.period:
            _add_term(out, self.period)
        return out

    def describe(self) -> dict[str, Any]:
        return {"question": self.question, "search_terms": list(self.search_terms),
                "period": self.period, "search_phrases": self.search_phrases()}


def _add_term(out: list[str], tok: str) -> None:
    tok = tok.strip("\"'()[]（）【】")
    if len(tok) < 2 or tok in _TERM_STOPWORDS or tok in out:
        return
    out.append(tok)


def parse_task_questions(task_profile: dict[str, Any] | None) -> list[TaskQuestion]:
    """``task_profile["questions"]`` -> validated list (invalid entries raise)."""
    raw = (task_profile or {}).get("questions")
    if raw in (None, "", []):
        return []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("task_profile.questions must be a list of strings or objects")
    out: list[TaskQuestion] = []
    for item in raw:
        q = TaskQuestion.from_value(item)
        if q is None:
            raise ValueError(f"task_profile.questions entry is empty or malformed: {item!r}")
        out.append(q)
    if len(out) > MAX_QUESTIONS:
        raise ValueError(f"task_profile.questions supports at most {MAX_QUESTIONS} questions")
    return out


def question_terms(questions: Iterable[TaskQuestion], exclude: Iterable[str] = ()) -> list[str]:
    """Union of question tokens, minus the target's own names (already entity terms)."""
    skip = {e.casefold() for e in exclude if e}
    out: list[str] = []
    for q in questions:
        for t in q.terms():
            if t.casefold() in skip or t in out:
                continue
            out.append(t)
    return out


def task_context(questions: Iterable[TaskQuestion]) -> dict[str, Any] | None:
    """Prompt block for the extraction model; ``None`` when the task has no questions."""
    items = [q.describe() for q in questions]
    if not items:
        return None
    return {
        "questions": [{"question": i["question"], "period": i["period"]} for i in items],
        "instruction": (
            "本次采集带有明确的研究问题。优先从来源中抽取能直接回答这些问题的 facts / metrics，"
            "每条都要写明报告期（period）、单位（unit）、口径（scope，如 归母/扣非、合并/母公司），"
            "同比等比较值放入 comparison。来源没有回答问题的部分不要推断或换算补齐，"
            "在 coverage_gaps 中说明缺失；来源若只给出近似/四舍五入数值，按原文数值与单位输出。"),
    }
