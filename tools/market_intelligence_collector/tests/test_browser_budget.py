"""T08/T13: RunBudget reservations, merge_limits, injectable clock, deadline."""

from __future__ import annotations

import pytest

from mic.budget import DEFAULT_LIMITS, BudgetExceeded, RunBudget, merge_limits
from tests.browser_doubles import FakeClock


def test_merge_limits_takes_min_of_deployment_and_task_and_ignores_unknown_task_keys():
    merged = merge_limits({"max_queries": 4, "max_search_pages_per_run": 6},
                          {"max_queries": 2, "max_search_pages_per_run": 10, "bogus": 99})
    assert merged["max_queries"] == 2
    assert merged["max_search_pages_per_run"] == 6
    assert "bogus" not in merged
    assert merged["max_links_to_read"] == DEFAULT_LIMITS["max_links_to_read"]


def test_reservation_happens_before_operation_and_failures_count():
    b = RunBudget(limits={**DEFAULT_LIMITS, "max_search_pages_per_query": 3, "max_search_pages_per_run": 6,
                          "max_engines_per_query": 2})
    b.reserve_search_page("q1", "bing")
    b.reserve_search_page("q1", "bing")    # a captcha page still counts
    b.reserve_search_page("q1", "google")
    ok, reason = b.can_open_search_page("q1", "bing")
    assert ok is False and reason == "max_search_pages_per_query"
    with pytest.raises(BudgetExceeded):
        b.reserve_search_page("q1", "bing")  # no implicit 4th page
    assert b.used_summary()["search_page_attempts"] == 3


def test_max_engines_per_query_is_enforced():
    b = RunBudget(limits={**DEFAULT_LIMITS, "max_engines_per_query": 1})
    b.reserve_search_page("q1", "bing")
    ok, reason = b.can_open_search_page("q1", "google")
    assert ok is False and reason == "max_engines_per_query"


def test_run_level_page_cap_spans_queries():
    b = RunBudget(limits={**DEFAULT_LIMITS, "max_search_pages_per_run": 2, "max_search_pages_per_query": 3})
    b.reserve_search_page("q1", "bing")
    b.reserve_search_page("q2", "bing")
    ok, reason = b.can_open_search_page("q3", "bing")
    assert ok is False and reason == "max_search_pages_per_run"


def test_gateway_requests_are_reserved_not_raised():
    b = RunBudget(limits={**DEFAULT_LIMITS, "max_gateway_requests": 1})
    b.reserve("gateway_requests_sent")
    with pytest.raises(BudgetExceeded) as exc:
        b.reserve("gateway_requests_sent")
    assert exc.value.counter == "gateway_requests_sent"
    assert b.used_summary()["gateway_requests_sent"] == 1


def test_deadline_uses_injected_clock():
    clock = FakeClock(100.0)
    b = RunBudget(limits={**DEFAULT_LIMITS, "max_run_seconds": 300, "max_page_seconds": 25},
                  clock=clock, deadline_at=clock() + 300)
    assert b.remaining_seconds() == 300
    assert b.page_timeout_seconds() == 25
    clock.advance(290)
    assert b.page_timeout_seconds() == 10  # bounded by remaining time
    clock.advance(20)
    assert b.expired() is True
    with pytest.raises(BudgetExceeded) as exc:
        b.reserve("http_read_attempts")
    assert exc.value.counter == "max_run_seconds"
    assert b.can_open_search_page("q", "bing") == (False, "run_deadline")


def test_cancel_blocks_every_reservation():
    b = RunBudget(limits=dict(DEFAULT_LIMITS))
    b.cancel("lease_lost")
    assert b.can("http_read_attempts") is False
    with pytest.raises(BudgetExceeded) as exc:
        b.reserve("search_hits")
    assert exc.value.counter == "cancelled"
    assert b.cancel_reason == "lease_lost"


def test_authenticated_retry_limits_per_origin_and_per_run():
    b = RunBudget(limits={**DEFAULT_LIMITS, "max_authenticated_retries_per_origin": 1,
                          "max_authenticated_retries_per_run": 1})
    b.reserve_authenticated_retry("https://www.bing.com")
    assert b.can_authenticated_retry("https://www.bing.com") == (False, "max_authenticated_retries_per_run")
    b2 = RunBudget(limits={**DEFAULT_LIMITS, "max_authenticated_retries_per_origin": 1,
                           "max_authenticated_retries_per_run": 5})
    b2.reserve_authenticated_retry("https://www.bing.com")
    assert b2.can_authenticated_retry("https://www.bing.com") == (False, "max_authenticated_retries_per_origin")
    assert b2.can_authenticated_retry("https://news.example.com") == (True, None)
