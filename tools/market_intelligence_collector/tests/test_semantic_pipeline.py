"""Offline pipeline integration: real prompts/planner/validator/MIC+Agent stores.

Search, reads and the model are deterministic doubles; no external requests.
"""
import copy
import json
from pathlib import Path

import pytest

from mic.modeling.adapter import ModelAdapter, ModelCallResult
from mic.modeling.mock_review import attach_mock_review
from mic.pipeline import Pipeline
from mic.planner import PlannedQuery
from mic.reader import ReadResult
from mic.schemas import Passage, SearchHit, TriageResult

FIXTURE = json.loads((Path(__file__).resolve().parents[3] /
    "agents/intelligence_collector_agent/tests/fixtures/semantic_event_961feb_live.json").read_text())


@pytest.mark.parametrize("budget", [1, 3])
def test_existing_model_calls_carry_context_then_persist_decisions(config, monkeypatch, tmp_path, budget):
    from agent_trade_intel.adapters.common import ToolResult
    from agent_trade_intel.db import SQLiteStore
    from agent_trade_intel.persistence import ResultPersister
    from agent_trade_intel.semantic_event_store import history

    config.raw["search_providers"]["active"] = "mock"
    config.raw["call_governance"]["batching"]["serp_batch_triage"] = False
    for policy in config.model_policies["tasks"].values():
        policy.update(call_mode="single_model", models=[{"model_id": "qwen_plus", "priority": 1}])
    for spec in config.model_registry["models"].values():
        spec["max_output_tokens"] = 65536
    pipe = Pipeline(config)
    originals = FIXTURE["events"]
    urls = [originals[0]["source"]["url"], originals[2]["source"]["url"]]
    monkeypatch.setattr(pipe.search, "search", lambda query, *args, **kw: [
        SearchHit(query=query, title="宁德时代储能系统采购中标", url=url, domain=url.split('/')[2], rank=i + 1,
                  query_family="orders_tender", provider="mock") for i, url in enumerate(urls)])
    monkeypatch.setattr(pipe.planner, "plan", lambda *a, **k: [PlannedQuery("宁德时代 中标", "orders_tender", 80)])
    monkeypatch.setattr(pipe.triage, "triage", lambda hit, link_id, **kw: TriageResult(
        source_link_id=link_id, triage_decision="read", read_priority=90, need_model=True))

    def read(link_id, url, *a, **kw):
        original = originals[0 if url == urls[0] else 2]
        return ReadResult(source_link_id=link_id, read_status="read", content_hash="body-" + str(urls.index(url)),
                          title="任丘智弘储能系统采购中标", http_status=200, content_length=241,
                          passages=[Passage(passage_id="title", section="标题", text="任丘智弘储能系统采购中标"),
                                    *[Passage(**p, section="正文") for p in original["source_context"]]])

    monkeypatch.setattr(pipe.reader, "read", read)
    requests = []

    def complete(adapter, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        meta = payload["source_metadata"]
        context = meta["event_resolution_context"]
        requests.append({"context": copy.deepcopy(context), "cap": adapter.max_output_tokens})
        first = meta["url"] == urls[0]
        events = copy.deepcopy(originals[:2] if first else originals[2:])
        for i, event in enumerate(events):
            evidence = [{"passage_id": event["evidence_locator"]["passage_id"],
                         "quote": event["evidence_locator"]["excerpt"]}]
            matching = None if first or i == 1 else context["candidates"][0 if i == 0 else 1]["ref"]
            event["event_resolution"] = {
                "reviewed": True, "verdict": "same_event" if matching else "new",
                "reason": "人工标注测试替身：同一标段中标，或不同标段/总体公告。",
                "current_evidence": evidence,
                "comparisons": [{"candidate_ref": c["ref"],
                                 "relation": "same_event" if c["ref"] == matching else "different",
                                 "reason": "人工标注测试替身：对照标段、获标方、公告上下文。",
                                 "current_evidence": evidence,
                                 "candidate_evidence": [{"passage_id": c["passages"][0]["passage_id"],
                                                         "quote": c["passages"][0]["text"]}]}
                                for c in context["candidates"]]}
        parsed = {"decision": "save_structured", "overall_score": 78, "confidence": .9, "events": events}
        parsed = attach_mock_review(parsed, payload["selected_passages"])
        return ModelCallResult(model_config_id=adapter.model_config_id, provider=adapter.provider,
                               provider_type=adapter.provider_type, model_name=adapter.model,
                               status="success", parsed=parsed, raw_text=json.dumps(parsed), is_mock=True)

    monkeypatch.setattr(ModelAdapter, "complete", complete)
    report = pipe.collect_intelligence("company_300750", {"focus": ["operating_update"],
        "budget_profile": {"max_queries": 1, "max_links_to_read": 2, "max_model_calls": budget, "max_gateway_requests": 3}})
    assert len(requests) == report["summary"]["model_calls"] == min(2, budget), report
    assert report["summary"]["gateway_requests_sent"] == 0
    assert all(r["cap"] == 65536 for r in requests)
    assert requests[0]["context"]["candidates"] == []
    if budget == 3:
        assert len(requests[1]["context"]["candidates"]) == 2
        intro = next(p["text"] for p in originals[0]["source_context"] if p["passage_id"] == "p0")
        for candidate in requests[1]["context"]["candidates"]:
            assert {p["passage_id"]: p["text"] for p in candidate["passages"]}["p0"] == intro
    assert all(e["event_resolution"]["status"] == "resolved" for e in report["all_events"])
    stored = pipe.repo.get_recent_events("company_300750")
    assert len(stored) == len(report["all_events"])
    assert all(e["source_context"] and e["event_resolution"]["event_fingerprint"] for e in stored)
    assert all({"title", "p0", e["evidence_locator"]["passage_id"]}
               <= {p["passage_id"] for p in e["source_context"]} for e in stored)
    store = SQLiteStore(tmp_path / "agent.db")
    store.init_schema()
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={},
                        status="success", result=report)
    counts = ResultPersister(store).save_mic_structures(task={"target": {"target_id": "company_300750",
        "company_name": "宁德时代"}}, result=result)
    assert (counts["events"], counts["events_linked"], counts["events_pending"]) == ((2, 0, 0) if budget == 1 else (3, 2, 0))
    next_cycle = history(store, "company_300750")
    assert len(next_cycle["candidates"]) == counts["events"]
    assert all({"title", "p0"} <= {p["passage_id"] for p in c["passages"]}
               for c in next_cycle["candidates"])
