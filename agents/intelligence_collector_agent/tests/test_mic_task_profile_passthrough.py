"""Config handoff into the MIC task profile. No search and no model call."""
from agent_trade_intel.agent import _mic_task_profile

QUESTIONS = [{
    "question": "整理产品形态、显存、功耗和互连。",
    "search_terms": ["产品 规格 官网", "互连 带宽"],
    "period": "当前公开资料",
    "note": "保留此字段",
}]
CONFIG = {"mic_task_defaults": {
    "deep_collect": {"focus": ["technology"], "time_window": "30d", "budget_profile": {"max_queries": 2}},
    "black_swan": {"focus": ["risk"], "time_window": "7d", "budget_profile": {"max_queries": 1}},
}}


def _task(task_type, mic):
    return {"task_type": task_type, "task_profile": {"mic": mic}}


def test_questions_reach_mic_unchanged_for_both_branches():
    for task_type in ("mic_deep_collect", "black_swan_scan"):
        original = [dict(QUESTIONS[0])]
        profile = _mic_task_profile(_task(task_type, {"questions": original, "time_window": "14d"}), CONFIG)
        assert profile["questions"] == QUESTIONS
        assert profile["questions"] is not original
        original[0]["question"] = "改写"
        assert profile["questions"][0]["question"] == QUESTIONS[0]["question"]


def test_omitted_questions_are_not_invented():
    profile = _mic_task_profile(_task("mic_deep_collect", {"time_window": "14d"}), CONFIG)
    assert "questions" not in profile


def test_normal_collection_window_uses_explicit_empty_and_default():
    explicit = _mic_task_profile(_task("mic_deep_collect", {"time_window": "14d", "questions": QUESTIONS}), CONFIG)
    empty = _mic_task_profile(_task("mic_deep_collect", {"time_window": "", "questions": QUESTIONS}), CONFIG)
    omitted = _mic_task_profile(_task("mic_deep_collect", {"questions": QUESTIONS}), CONFIG)
    assert explicit["time_window"] == "14d"
    assert empty["time_window"] == ""
    assert omitted["time_window"] == "30d"
    assert explicit["questions"] == empty["questions"] == omitted["questions"] == QUESTIONS


def test_black_swan_window_priority_keeps_explicit_empty_strings():
    both = _mic_task_profile(_task("black_swan_scan", {
        "black_swan_time_window": "2d", "time_window": "90d", "questions": QUESTIONS}), CONFIG)
    swan_empty = _mic_task_profile(_task("black_swan_scan", {
        "black_swan_time_window": "", "time_window": "90d", "questions": QUESTIONS}), CONFIG)
    only_window_empty = _mic_task_profile(_task("black_swan_scan", {
        "time_window": "", "questions": QUESTIONS}), CONFIG)
    omitted = _mic_task_profile(_task("black_swan_scan", {"questions": QUESTIONS}), CONFIG)
    assert both["time_window"] == "2d"
    assert swan_empty["time_window"] == ""
    assert only_window_empty["time_window"] == ""
    assert omitted["time_window"] == "7d"
    assert all(profile["questions"] == QUESTIONS for profile in (both, swan_empty, only_window_empty, omitted))


def test_absent_default_window_uses_the_branch_fallback():
    profile = _mic_task_profile(_task("mic_deep_collect", {}), {"mic_task_defaults": {}})
    swan = _mic_task_profile(_task("black_swan_scan", {}), {"mic_task_defaults": {}})
    assert profile["time_window"] == "30d"
    assert swan["time_window"] == "7d"
