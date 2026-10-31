"""Bounded re-extraction through the real adapter and an in-memory SDK.

These checks establish execution and budget behavior, not model understanding.
No SDK constructor, network request, source read or real collection is used.
"""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from mic.budget import RunBudget
from mic.config import MICConfig
from mic.modeling import adapter as adapter_module
from mic.modeling.acceptance_trace import AcceptanceTrace
from mic.modeling.adapter import ModelAdapter
from mic.modeling.call_planner import CallBudget, ModelCallPlanner
from mic.modeling.prompts import build_bundle_messages, build_json_recovery_messages
from mic.profile import TargetProfile
from mic.reader import ReadResult
from mic.schemas import Passage, TriageResult


BAD = '{"private_bad_output":"BROKEN_RESPONSE_PRIVATE_MARKER"'
REVIEW = {"protocol": "content_review_v2", "claims": [], "bindings": {}}
GOOD = json.dumps({"schema_version": "bundle_extraction_v0.3", "decision": "save_structured",
                   "overall_score": 72, "confidence": 0.9, "content_review": REVIEW,
                   "facts": [{"fact_statement": "仅供离线调用回归。"}]}, ensure_ascii=False)
VALID_SKIP = json.dumps({"schema_version": "bundle_extraction_v0.3", "decision": "skip",
                         "overall_score": 0, "confidence": 0.0, "content_review": REVIEW})
# r3 link_4b4f7207e96c: the review object alone at top level, no bundle fields.
REVIEW_ONLY = json.dumps({"protocol": "content_review_v2",
                          "decision_reason": "PRIVATE_DRAFT_MARKER 属70-84档。",
                          "claims": [{"id": "rev", "kind": "observation", "status": "source_supported",
                                      "statement": "PRIVATE_CLAIM_MARKER"}],
                          "bindings": {"/metrics/0": ["rev"]}, "task_coverage": []}, ensure_ascii=False)


def _setup(monkeypatch, responses, *, mode="single_model", models=1,
           run_limit=8, source_limit=3, gateway_limit=8, enabled=None):
    # Each request consumes a scripted reply. The real adapter parses it and
    # reserves the actual Gateway send, while the fake can advance the clock.
    calls, queue = [], list(responses)
    clock = [100.0]
    runtime = RunBudget(limits={"max_gateway_requests": gateway_limit,
                                "max_model_calls": run_limit, "max_run_seconds": 60},
                        clock=lambda: clock[0])
    adapters = {}

    def client_for(model_id):
        def create(**kwargs):
            calls.append({"adapter_id": model_id, **deepcopy(kwargs)})
            assert queue, "Unexpected extra SDK request"
            reply = queue.pop(0)
            if isinstance(reply, str):
                reply = {"raw": reply}
            clock[0] += reply.get("elapsed", 0)
            if reply.get("cancel"):
                runtime.cancel("fixture_cancel")
            if reply.get("error"):
                raise RuntimeError("offline scripted request failure")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply["raw"]),
                                         finish_reason=reply.get("finish_reason", "stop"))],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=25),
                model="offline-served-model", id=f"offline-response-{len(calls)}")
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def prohibited_constructor(*args, **kwargs):
        raise AssertionError("Tests must never construct a live SDK client")

    monkeypatch.setattr(adapter_module, "OpenAI", prohibited_constructor)
    model_specs = []
    for index in range(models):
        model_id = f"offline_{index}"
        adapter = ModelAdapter(
            model_config_id=model_id, provider="offline", provider_type="openai_compatible_direct",
            endpoint="http://127.0.0.1:1/v1", model=f"offline-requested-{index}",
            api_key="offline-recovery-fixture-credential", allow_mock=False, max_output_tokens=65536)
        adapter._client = client_for(model_id)
        adapter.set_budget(runtime)
        adapters[model_id] = adapter
        model_specs.append({"model_id": model_id, "priority": index, "weight": 1.0})
    extraction = {"split_tasks_only_when": ["selected_text_too_long"]}
    if enabled is not None:
        extraction["json_recovery"] = {"enabled": enabled}
    config = MICConfig(raw={
        "model_policies": {"tasks": {
            "bundle_extraction": {"call_mode": mode, "models": model_specs},
            "arbitration": {"call_mode": "priority_fallback", "models": model_specs},
            "serp_batch_triage": {"call_mode": "batch_triage", "models": model_specs}}},
        "call_governance": {"extraction": extraction},
    })
    planner = ModelCallPlanner(config, SimpleNamespace(get=adapters.get),
                               CallBudget(max_model_calls_per_run=run_limit,
                                          max_model_calls_per_source_link=source_limit))
    profile = TargetProfile(target_id="fixture", canonical_name="测试主体", type="company")
    return SimpleNamespace(planner=planner, runtime=runtime, calls=calls, queue=queue,
                           clock=clock, profile=profile, adapters=adapters)


def _input(source="link_fixture", passages=None):
    if passages is None:
        passages = [Passage(passage_id="p0", section="正文", text="本来源给出的原始材料。")]
    read = ReadResult(source_link_id=source, read_status="read", passages=passages)
    metadata = {"source_link_id": source, "title": "离线材料", "url": "https://example.invalid/a"}
    triage = TriageResult(source_link_id=source, triage_decision="read", read_priority=70, need_model=True)
    return read, metadata, triage


def _extract(run, source="link_fixture", passages=None):
    read, metadata, triage = _input(source, passages)
    return run.planner.run_for_link(run.profile, read, metadata, triage, "media", 70)


def _enable_trace(run, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    logs = repo / "tools/market_intelligence_collector/logs/acceptance/recovery"
    logs.mkdir(parents=True)
    active = logs / "run_recovery.log"
    active.write_text("")
    monkeypatch.setenv("MIC_ACCEPTANCE_MODEL_IO", "1")
    monkeypatch.setenv("MIC_LOG_DIR", str(logs))
    monkeypatch.setenv("MIC_ACTIVE_LOG_FILE", str(active))
    run.planner.acceptance_trace = AcceptanceTrace.from_environment("run_recovery", 8, repo_root=repo)
    return logs


def test_recovery_preserves_material_and_records_two_linked_attempts(tmp_path, monkeypatch):
    run = _setup(monkeypatch, [BAD, {"raw": GOOD, "elapsed": 2}])
    logs = _enable_trace(run, tmp_path, monkeypatch)
    deadline, started = run.runtime.deadline_at, run.runtime.started_at
    read, metadata, triage = _input()
    expected = build_bundle_messages(run.profile, metadata, read.passages, run.planner.output_limits)
    result = run.planner.run_for_link(run.profile, read, metadata, triage, "media", 70)
    parent, child = result.outputs
    assert parent.status == "json_invalid" and parent.raw_text == BAD and parent.parsed is None
    assert child.status == "success" and child.raw_text == GOOD and child.parsed == json.loads(GOOD)
    assert run.calls[0]["messages"] == expected
    assert [m for m in run.calls[1]["messages"] if m["role"] == "user"] == [expected[1]]
    assert "BROKEN_RESPONSE_PRIVATE_MARKER" not in json.dumps(run.calls[1]["messages"])
    assert run.calls[0]["adapter_id"] == run.calls[1]["adapter_id"]
    assert len(run.calls) == run.planner.budget.calls_used == run.runtime.used["gateway_requests_sent"] == 2
    assert run.runtime.deadline_at == deadline and run.runtime.started_at == started
    assert all(c["max_tokens"] == 65536 and c["response_format"] == {"type": "json_object"}
               for c in run.calls)
    association = {"strategy": "reextract_original_input", "attempt": 1, "trigger": "json_invalid",
                   "parent_response_sha256": sha256(BAD.encode()).hexdigest(), "parent_output_index": 0,
                   "parent_trace_id": parent.acceptance_trace["trace_id"],
                   "parent_provider_request_id": parent.provider_request_id}
    assert child.json_recovery == association
    assert child.request_diagnostics()["json_recovery"] == association
    assert parent.json_recovery is None
    assert parent.acceptance_trace["trace_id"] != child.acceptance_trace["trace_id"]
    for output in result.outputs:
        directory = logs / output.acceptance_trace["relative_path"]
        assert (directory / "response.txt").read_bytes() == output.raw_text.encode()
        saved = json.loads((directory / "result.json").read_text())
        assert saved["status"] == output.status
        if output is child:
            assert saved["json_recovery"] == association
        else:
            assert "json_recovery" not in saved


def test_recovery_prompt_does_not_mutate_or_duplicate_32000_character_material():
    user = json.dumps({"selected_passages": [{"passage_id": "p0", "text": "原" * 32000}]}, ensure_ascii=False)
    messages = [{"role": "system", "content": "existing shared review policy"},
                {"role": "user", "content": user}]
    original = deepcopy(messages)
    regenerated = build_json_recovery_messages(messages)
    assert messages == original
    assert regenerated is not messages
    system = next(m["content"] for m in regenerated if m["role"] == "system")
    assert system.startswith(messages[0]["content"])
    assert 0 < len(system) - len(messages[0]["content"]) < 1000
    assert [m for m in regenerated if m["role"] == "user"] == [messages[1]]
    assert "BROKEN_RESPONSE_PRIVATE_MARKER" not in json.dumps(regenerated)
    regenerated[0]["content"] = "mutated copy"
    assert messages == original


def test_second_invalid_response_stops_and_recovery_is_once_for_entire_run(monkeypatch):
    run = _setup(monkeypatch, [BAD, BAD, BAD], source_limit=3)
    first = _extract(run, "link_first")
    second = _extract(run, "link_second")
    assert [r.status for r in first.outputs] == ["json_invalid", "json_invalid"]
    assert [r.status for r in second.outputs] == ["json_invalid"]
    assert len(run.calls) == run.planner.budget.calls_used == run.runtime.used["gateway_requests_sent"] == 3


def test_recovery_can_be_disabled(monkeypatch):
    run = _setup(monkeypatch, [BAD], enabled=False)
    assert len(_extract(run).outputs) == len(run.calls) == 1


@pytest.mark.parametrize("limit", ["run", "source", "gateway"])
def test_exhausted_budget_prevents_recovery_send(monkeypatch, limit):
    options = {"run_limit": 1} if limit == "run" else {"source_limit": 1} if limit == "source" else {"gateway_limit": 1}
    run = _setup(monkeypatch, [BAD], **options)
    result = _extract(run)
    assert len(result.outputs) == len(run.calls) == 1
    assert run.runtime.used["gateway_requests_sent"] == run.planner.budget.calls_used == 1


@pytest.mark.parametrize("reply", [{"raw": BAD, "elapsed": 60}, {"raw": BAD, "cancel": True}])
def test_deadline_or_cancellation_between_attempts_prevents_recovery(monkeypatch, reply):
    run = _setup(monkeypatch, [reply])
    assert len(_extract(run).outputs) == len(run.calls) == 1
    assert run.runtime.used["gateway_requests_sent"] == 1


def test_real_adapter_timeout_uses_original_remaining_deadline(monkeypatch):
    run = _setup(monkeypatch, [{"raw": BAD, "elapsed": 7}, GOOD])
    deadline = run.runtime.deadline_at
    _extract(run)
    assert [c["timeout"] for c in run.calls] == [60.0, 53.0]
    assert run.runtime.deadline_at == deadline


@pytest.mark.parametrize("mode", ["single_model", "priority_fallback", "cascade", "parallel_ensemble"])
def test_recovery_is_bounded_after_each_ordinary_extraction_policy(monkeypatch, mode):
    run = _setup(monkeypatch, [BAD, GOOD], mode=mode)
    assert [r.status for r in _extract(run).outputs] == ["json_invalid", "success"]
    assert len(run.calls) == 2


def test_existing_fallback_success_prevents_extra_recovery(monkeypatch):
    run = _setup(monkeypatch, [BAD, GOOD], mode="priority_fallback", models=2)
    result = _extract(run)
    assert [r.status for r in result.outputs] == ["json_invalid", "success"]
    assert all(r.json_recovery is None for r in result.outputs)
    assert [c["adapter_id"] for c in run.calls] == ["offline_0", "offline_1"]


def test_last_failed_fallback_is_reextracted_once_with_same_adapter(monkeypatch):
    run = _setup(monkeypatch, [BAD, BAD, GOOD], mode="priority_fallback", models=2)
    result = _extract(run)
    assert [c["adapter_id"] for c in run.calls] == ["offline_0", "offline_1", "offline_1"]
    assert result.outputs[-1].json_recovery["parent_output_index"] == 1
    assert result.outputs[-1].json_recovery["parent_provider_request_id"] == "offline-response-2"
    assert len(result.outputs) == run.planner.budget.calls_used == 3


def test_prior_success_even_below_early_stop_prevents_recovery(monkeypatch):
    weak = json.dumps({"schema_version": "bundle_extraction_v0.3", "decision": "link_only",
                       "overall_score": 12, "confidence": 0.1, "content_review": REVIEW})
    run = _setup(monkeypatch, [weak, BAD], mode="priority_fallback", models=2)
    assert [r.status for r in _extract(run).outputs] == ["success", "json_invalid"]
    assert len(run.calls) == 2


@pytest.mark.parametrize("reply,expected", [
    ({"raw": GOOD, "finish_reason": "length"}, "output_truncated"),
    ({"error": True}, "request_failed"),
    (VALID_SKIP, "success"),
])
def test_transport_failures_and_valid_skip_do_not_trigger_recovery(monkeypatch, reply, expected):
    run = _setup(monkeypatch, [reply])
    assert [r.status for r in _extract(run).outputs] == [expected]
    assert len(run.calls) == 1


def test_protocol_invalid_object_is_reextracted_once_with_error_paths_only(monkeypatch):
    run = _setup(monkeypatch, [REVIEW_ONLY, GOOD])
    result = _extract(run)
    parent, child = result.outputs
    assert parent.status == "success" and parent.raw_text == REVIEW_ONLY
    assert parent.parsed == json.loads(REVIEW_ONLY)
    assert child.json_recovery["trigger"] == "response_protocol_invalid"
    assert child.json_recovery["protocol_errors"] == [
        "schema_version: missing", "decision: missing", "overall_score: missing",
        "confidence: missing", "content_review: missing"]
    retry = json.dumps(run.calls[1]["messages"], ensure_ascii=False)
    assert "overall_score: missing" in retry
    assert "PRIVATE_CLAIM_MARKER" not in retry and "PRIVATE_DRAFT_MARKER" not in retry
    assert len(run.calls) == run.planner.budget.calls_used == 2


def test_json_and_protocol_recovery_share_one_quota(monkeypatch):
    run = _setup(monkeypatch, [BAD, REVIEW_ONLY, "{}"], source_limit=3)
    first = _extract(run, "link_first")
    second = _extract(run, "link_second")
    assert [r.status for r in first.outputs] == ["json_invalid", "success"]
    assert first.outputs[-1].json_recovery["trigger"] == "json_invalid"
    assert [r.status for r in second.outputs] == ["success"]
    assert second.outputs[0].json_recovery is None
    assert len(run.calls) == run.planner.budget.calls_used == run.runtime.used["gateway_requests_sent"] == 3


def test_second_protocol_invalid_response_stops(monkeypatch):
    run = _setup(monkeypatch, [REVIEW_ONLY, "{}"], source_limit=3)
    outputs = _extract(run).outputs
    assert [r.json_recovery is not None for r in outputs] == [False, True]
    assert len(run.calls) == 2


@pytest.mark.parametrize("limit", ["run", "source", "gateway"])
def test_exhausted_budget_prevents_protocol_recovery_send(monkeypatch, limit):
    options = {"run_limit": 1} if limit == "run" else {"source_limit": 1} if limit == "source" else {"gateway_limit": 1}
    run = _setup(monkeypatch, [REVIEW_ONLY], **options)
    assert len(_extract(run).outputs) == len(run.calls) == 1


@pytest.mark.parametrize("reply", [{"raw": REVIEW_ONLY, "elapsed": 60}, {"raw": REVIEW_ONLY, "cancel": True}])
def test_deadline_or_cancellation_prevents_protocol_recovery(monkeypatch, reply):
    run = _setup(monkeypatch, [reply])
    assert len(_extract(run).outputs) == len(run.calls) == 1


def test_arbitration_protocol_invalid_gets_no_recovery(monkeypatch):
    run = _setup(monkeypatch, [REVIEW_ONLY])
    read, metadata, _ = _input()
    outputs = run.planner.arbitrate(run.profile, read, metadata, [{"field": "amount", "values": [1, 2]}])
    assert [r.status for r in outputs] == ["success"]
    assert len(run.calls) == 1


def test_earlier_invalid_does_not_recover_after_last_transport_failure(monkeypatch):
    run = _setup(monkeypatch, [BAD, {"error": True}], mode="priority_fallback", models=2)
    assert [r.status for r in _extract(run).outputs] == ["json_invalid", "request_failed"]
    assert len(run.calls) == 2


@pytest.mark.parametrize("operation", ["dispatch", "triage", "arbitration"])
def test_non_extraction_entrypoints_never_add_recovery(monkeypatch, operation):
    run = _setup(monkeypatch, [BAD])
    planner = run.planner
    if operation == "dispatch":
        policy = planner.policies["bundle_extraction"]
        outputs = planner._dispatch("single_model", policy, [{"role": "user", "content": "{}"}])
    elif operation == "triage":
        planner.batch_triage([{"id": "candidate", "title": "离线候选", "snippet": "候选摘要"}])
        outputs = planner.triage_results
    else:
        read, metadata, _ = _input()
        outputs = planner.arbitrate(run.profile, read, metadata, [{"field": "amount", "values": [1, 2]}])
    assert [r.status for r in outputs] == ["json_invalid"]
    assert len(run.calls) == 1


def test_split_extraction_has_no_recovery_and_respects_source_cap(monkeypatch):
    run = _setup(monkeypatch, [BAD, BAD], source_limit=2)
    run.planner.max_input_chars = 40
    passages = [Passage(passage_id=f"p{i}", section="正文", text="分" * 30) for i in range(4)]
    result = _extract(run, passages=passages)
    assert result.was_split
    assert [r.status for r in result.outputs] == ["json_invalid", "json_invalid"]
    assert len(run.calls) == run.planner.budget.calls_used == 2


def test_one_remaining_source_slot_preserves_split_material_boundary(monkeypatch):
    run = _setup(monkeypatch, [BAD], source_limit=1)
    run.planner.max_input_chars = 40
    passages = [Passage(passage_id=f"p{i}", section="正文", text=character * 30)
                for i, character in enumerate(("甲", "乙"))]
    result = _extract(run, passages=passages)
    assert result.was_split
    assert [r.status for r in result.outputs] == ["json_invalid"]
    assert len(run.calls) == run.planner.budget.calls_used == 1
    payload = json.loads(next(m["content"] for m in run.calls[0]["messages"] if m["role"] == "user"))
    assert payload["selected_passages"] == [passages[0].model_dump()]
    assert sum(len(p["text"]) for p in payload["selected_passages"]) == 30


def test_zero_source_budget_prevents_single_model_send(monkeypatch):
    run = _setup(monkeypatch, [], source_limit=0)
    assert not _extract(run).outputs
    assert not run.calls and run.planner.budget.calls_used == 0


def test_extraction_recovery_and_arbitration_share_source_quota(monkeypatch):
    run = _setup(monkeypatch, [BAD, GOOD], source_limit=2)
    assert len(_extract(run).outputs) == 2
    read, metadata, _ = _input()
    assert run.planner.arbitrate(run.profile, read, metadata, [{"field": "amount", "values": [1, 2]}]) == []
    assert not _extract(run).outputs
    assert len(run.calls) == run.planner.budget.calls_used == 2


def test_remaining_source_slot_allows_one_arbitration_without_recovery(monkeypatch):
    run = _setup(monkeypatch, [BAD, GOOD, BAD], source_limit=3)
    assert len(_extract(run).outputs) == 2
    read, metadata, _ = _input()
    outputs = run.planner.arbitrate(run.profile, read, metadata, [{"field": "amount", "values": [1, 2]}])
    assert [r.status for r in outputs] == ["json_invalid"]
    assert len(run.calls) == run.planner.budget.calls_used == 3
