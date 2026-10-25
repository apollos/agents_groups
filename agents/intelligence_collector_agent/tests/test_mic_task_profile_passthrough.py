"""Config handoff into the MIC task profile. No search and no model call."""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

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


@pytest.mark.parametrize("task_type", ["mic_deep_collect", "black_swan_scan"])
def test_partial_test_cap_keeps_task_transport_limits_and_deadline(task_type):
    config = deepcopy(CONFIG)
    config["mic_task_defaults"]["test_mode_budget_profile"] = {
        "max_queries": 2, "max_links_to_read": 2, "max_model_calls": 3,
    }
    requested = {
        "max_queries": 4, "max_links_to_read": 1, "max_model_calls": 1,
        "max_http_read_attempts": 1, "max_browser_read_attempts": 1,
        "max_gateway_requests": 1, "max_run_seconds": 45,
    }
    task = {**_task(task_type, {"budget_profile": requested}), "test_mode": True}
    original_task, original_config = deepcopy(task), deepcopy(config)

    actual = _mic_task_profile(task, config)["budget_profile"]

    assert actual == {**requested, "max_queries": 2}
    assert task == original_task and config == original_config


def test_test_cap_supplies_missing_limits_and_empty_cap_preserves_request():
    requested = {"max_http_read_attempts": 1, "max_run_seconds": 45}
    task = {**_task("mic_deep_collect", {"budget_profile": requested}), "test_mode": True}
    config = deepcopy(CONFIG)
    cap = {"max_queries": 2, "max_links_to_read": 2, "max_model_calls": 3}
    config["mic_task_defaults"]["test_mode_budget_profile"] = cap
    assert _mic_task_profile(task, config)["budget_profile"] == {**requested, **cap}
    config["mic_task_defaults"]["test_mode_budget_profile"] = {}
    assert _mic_task_profile(task, config)["budget_profile"] == requested


def test_repo_profiles_fit_deployment_and_quick_checks_are_explicitly_bounded():
    """No nominal Agent budget should silently exceed the shipped MIC deployment ceiling."""
    repo = Path(__file__).resolve().parents[3]
    config = yaml.safe_load((repo / "agents/intelligence_collector_agent/config/intelligence_collector.yaml")
                            .read_text())
    deployment = yaml.safe_load((repo / "tools/market_intelligence_collector/config/browser_runtime.yaml")
                                .read_text())["browser_runtime"]["limits"]
    deep = _mic_task_profile(_task("mic_deep_collect", {}), config)["budget_profile"]
    assert deep == {key: deployment[key] for key in deep}
    assert config["tools"]["market_intelligence_collector"]["timeout_seconds"] == deep["max_run_seconds"]

    quick = config["mic_task_defaults"]["test_mode_budget_profile"]
    assert quick["max_model_calls"] >= quick["max_links_to_read"] + 1
    for task_type in ("mic_deep_collect", "black_swan_scan"):
        normal = _mic_task_profile(_task(task_type, {}), config)["budget_profile"]
        task = {**_task(task_type, {}), "test_mode": True}
        actual = _mic_task_profile(task, config)["budget_profile"]
        assert all(value <= deployment[key] for key, value in normal.items())
        assert actual == {key: min(normal[key], limit) for key, limit in quick.items()}


def test_non_test_task_budget_override_is_preserved_for_mic_deployment_clamping():
    requested = {"max_http_read_attempts": 80, "max_run_seconds": 90}
    profile = _mic_task_profile(_task("mic_deep_collect", {"budget_profile": requested}), CONFIG)
    assert profile["budget_profile"] == requested
