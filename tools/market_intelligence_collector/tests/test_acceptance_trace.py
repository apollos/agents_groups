"""Synthetic acceptance capture: no network, parsing repair or extra calls."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mic.budget import RunBudget
from mic.config import MICConfig
from mic.modeling import acceptance_trace as trace_module
from mic.modeling.acceptance_trace import AcceptanceTrace
from mic.modeling.adapter import ModelCallResult
from mic.modeling.call_planner import CallBudget, ModelCallPlanner
from mic.profile import TargetProfile
from mic.schemas import Passage


def messages(text="2026年9月15日公告金额123元。", source="link_a"):
    return [{"role": "system", "content": "PRIVATE SYSTEM PROMPT: do not persist this instruction."},
            {"role": "user", "content": json.dumps({
                "task": "bundle_extraction", "target_profile": {"configuration": "not-a-material"},
                "output_limits": {"not_a_material": 30}, "output_schema_hint": {"not_a_material": True},
                "source_metadata": {"source_link_id": source, "title": "公告", "url": "https://example.invalid/a"},
                "selected_passages": [{"passage_id": "p1", "section": "正文", "text": text}],
                "task_context": {"questions": [{"question": "查询披露日期与金额"}], "reference_time": "2026-10-20"},
            }, ensure_ascii=False)}]


class Adapter:
    api_key = "local-fixture-credential-value"
    usable = True

    def __init__(self, raw='{"bad":\n unquoted}', *, budget=None):
        self.raw, self.budget = raw, budget
        self.calls = []

    def complete(self, request):
        self.calls.append(deepcopy(request))
        if self.budget is not None:
            self.budget.reserve("gateway_requests_sent")
        return ModelCallResult("fixture", "fixture", "offline", "fixture/model", "json_invalid",
                               raw_text=self.raw, output_tokens=28761, requested_max_tokens=65536,
                               finish_reason="stop", provider_request_id="response-fixture")


def enabled(tmp_path, monkeypatch, *, component="agents/intelligence_collector_agent", max_calls=8, run_id="run_fixture"):
    repo = tmp_path / "repo"
    logs = repo / component / "logs/acceptance/batch/logs/mic"
    logs.mkdir(parents=True)
    active = logs / (run_id + ".log")
    active.write_text("")
    monkeypatch.setenv("MIC_ACCEPTANCE_MODEL_IO", "1")
    monkeypatch.setenv("MIC_LOG_DIR", str(logs))
    monkeypatch.setenv("MIC_ACTIVE_LOG_FILE", str(active))
    return AcceptanceTrace.from_environment(run_id, max_calls, repo_root=repo), repo, logs


def directory(logs, result):
    return logs / result.acceptance_trace["relative_path"]


@pytest.mark.parametrize("flag", [None, "0", "true", ""])
def test_default_or_nonexact_flag_writes_nothing_and_changes_no_diagnostics(tmp_path, monkeypatch, flag):
    monkeypatch.delenv("MIC_ACCEPTANCE_MODEL_IO", raising=False)
    if flag is not None:
        monkeypatch.setenv("MIC_ACCEPTANCE_MODEL_IO", flag)
    before = list(tmp_path.rglob("*"))
    recorder = AcceptanceTrace.from_environment("run_x", 8, repo_root=tmp_path)
    adapter = Adapter()
    result = recorder.complete(adapter, messages())
    assert len(adapter.calls) == 1 and result.acceptance_trace is None
    assert result.request_diagnostics() == {"requested_max_tokens": 65536, "finish_reason": "stop",
        "served_model": None, "is_mock": False, "output_truncated": False}
    assert list(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("component", ["agents/intelligence_collector_agent", "tools/market_intelligence_collector"])
def test_actual_component_log_layout_saves_invalid_json_without_repair(tmp_path, monkeypatch, component):
    recorder, _, logs = enabled(tmp_path, monkeypatch, component=component)
    adapter = Adapter()
    request = messages()
    before = deepcopy(request)
    result = recorder.complete(adapter, request)
    assert result.status == "json_invalid" and result.parsed is None
    assert result.raw_text == adapter.raw and request == before and adapter.calls == [before]
    folder = directory(logs, result)
    assert (folder / "response.txt").read_text() == adapter.raw
    saved = json.loads((folder / "input.json").read_text())["user_material"][0]
    assert saved["selected_passages"] == json.loads(request[1]["content"])["selected_passages"]
    assert "target_profile" not in saved and "output_schema_hint" not in saved and "output_limits" not in saved
    metadata = json.loads((folder / "result.json").read_text())
    assert metadata["status"] == "json_invalid" and metadata["finish_reason"] == "stop"
    assert metadata["response"]["raw_sha256"] == sha256(adapter.raw.encode()).hexdigest()
    assert metadata["response"]["complete"] and not metadata["response"]["redacted"]
    assert metadata["requested_max_tokens"] == 65536 and metadata["output_tokens"] == 28761
    assert result.request_diagnostics()["acceptance_trace"] == result.acceptance_trace


@pytest.mark.parametrize("damage,reason", [
    ("missing_log_dir", "explicit_absolute_log_dir_required"),
    ("relative_log_dir", "explicit_absolute_log_dir_required"),
    ("outside_logs", "outside_component_logs"),
    ("wrong_active", "active_run_log_mismatch"),
    ("other_run", "active_run_log_mismatch"),
    ("traversal", "invalid_run_id"),
    ("symlink_escape", "outside_component_logs"),
])
def test_explicit_invalid_environment_is_visible_but_does_not_change_call(tmp_path, monkeypatch, damage, reason):
    _, repo, logs = enabled(tmp_path, monkeypatch)
    run_id = "run_fixture"
    if damage == "missing_log_dir":
        monkeypatch.delenv("MIC_LOG_DIR")
    elif damage == "relative_log_dir":
        monkeypatch.setenv("MIC_LOG_DIR", "logs")
    elif damage == "outside_logs":
        monkeypatch.setenv("MIC_LOG_DIR", str(repo / "config"))
    elif damage == "wrong_active":
        monkeypatch.setenv("MIC_ACTIVE_LOG_FILE", str(repo / "elsewhere.log"))
    elif damage == "other_run":
        run_id = "run_other"
    elif damage == "traversal":
        run_id = "../outside"
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        link = repo / "agents/intelligence_collector_agent/logs/escape"
        link.symlink_to(outside, target_is_directory=True)
        monkeypatch.setenv("MIC_LOG_DIR", str(link))
    recorder = AcceptanceTrace.from_environment(run_id, 8, repo_root=repo)
    adapter = Adapter()
    result = recorder.complete(adapter, messages())
    assert result.acceptance_trace == {"enabled": False, "reason": reason}
    assert len(adapter.calls) == 1 and result.raw_text == adapter.raw
    assert not (logs / "acceptance_model_io").exists()


def test_credentials_config_and_system_echo_are_redacted_without_touching_model_values(tmp_path, monkeypatch):
    recorder, _, logs = enabled(tmp_path, monkeypatch)
    monkeypatch.setenv("TRACE_PRIVATE_TOKEN", "private-env-token-value")
    request = messages()
    system = request[0]["content"]
    payload = json.loads(request[1]["content"])
    payload["selected_passages"][0]["text"] += " " + Adapter.api_key + " " + system
    payload["source_metadata"]["url"] = "https://private-url-user:private-url-pass@example.invalid/a?access_token=private-url-value&visible=1"
    payload["source_metadata"]["headers"] = {"Cookie": "private-config-cookie"}
    payload["config"] = {"secret": "private-config-value"}
    request[1]["content"] = json.dumps(payload, ensure_ascii=False)
    raw = ('{"broken":' + Adapter.api_key + " private-env-token-value\n"
           + system + '\n{"api_key":"private-response-key","cookie":"private-response-cookie"}\n'
           + 'Authorization: Bearer private-header-token\n')
    adapter = Adapter(raw)
    result = recorder.complete(adapter, request)
    assert result.raw_text == raw and adapter.calls == [request]
    saved = "\n".join(p.read_text() for p in directory(logs, result).iterdir())
    for secret in (Adapter.api_key, "private-env-token-value", system, "private-url-value", "private-config-cookie",
                   "private-config-value", "private-response-key", "private-response-cookie", "private-header-token",
                   "private-url-user", "private-url-pass"):
        assert secret not in saved
    assert "123元" in saved and "visible=1" in saved
    assert result.acceptance_trace["input_redacted"] and result.acceptance_trace["response_redacted"]


def test_input_over_limit_is_explicitly_omitted_and_not_cut_into_fake_passages(tmp_path, monkeypatch):
    recorder, _, logs = enabled(tmp_path, monkeypatch)
    result = recorder.complete(Adapter(), messages("汉" * trace_module.INPUT_LIMIT))
    folder = directory(logs, result)
    assert result.acceptance_trace["input_status"] == "omitted_input_limit"
    assert not (folder / "input.json").exists()
    assert (folder / "response.txt").is_file()


def test_response_over_limit_keeps_labelled_utf8_segments_and_offsets(tmp_path, monkeypatch):
    recorder, _, logs = enabled(tmp_path, monkeypatch)
    raw = "开头" + "汉字" * 400000 + "结尾"
    result = recorder.complete(Adapter(raw), messages())
    folder = directory(logs, result)
    head, tail = (folder / "response.head.txt").read_bytes(), (folder / "response.tail.txt").read_bytes()
    assert raw.encode().startswith(head) and raw.encode().endswith(tail)
    assert len(head) + len(tail) <= trace_module.RESPONSE_LIMIT
    assert not (folder / "response.txt").exists() and not result.acceptance_trace["response_complete"]
    detail = json.loads((folder / "result.json").read_text())["response"]
    assert detail["head_end_byte"] == len(head) and detail["tail_start_byte"] == len(raw.encode()) - len(tail)
    assert detail["omitted_bytes"] == len(raw.encode()) - len(head) - len(tail)


def test_trace_call_limit_is_capped_without_capping_model_calls(tmp_path, monkeypatch):
    recorder, _, logs = enabled(tmp_path, monkeypatch, max_calls=30)
    adapter = Adapter()
    results = [recorder.complete(adapter, messages()) for _ in range(17)]
    assert recorder.max_calls == 16 and len(adapter.calls) == 17
    assert len(list((logs / "acceptance_model_io/run_fixture").iterdir())) == 16
    assert results[-1].acceptance_trace["reason"] == "trace_call_limit"
    assert all(result.status == "json_invalid" for result in results)


def test_total_bytes_and_write_failure_never_change_model_response(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_module, "TOTAL_LIMIT", 64 * 1024)
    monkeypatch.setattr(trace_module, "RESPONSE_LIMIT", 32 * 1024)
    monkeypatch.setattr(trace_module, "METADATA_LIMIT", 1024)
    recorder, _, logs = enabled(tmp_path, monkeypatch, max_calls=4)
    adapter = Adapter("x" * (32 * 1024))
    results = [recorder.complete(adapter, messages()) for _ in range(4)]
    assert len(adapter.calls) == 4 and all(r.raw_text == adapter.raw for r in results)
    assert any(r.acceptance_trace["status"] == "write_failed" for r in results)
    assert sum(p.stat().st_size for p in (logs / "acceptance_model_io").rglob("*") if p.is_file()) <= 64 * 1024


def test_os_write_failure_is_recorded_but_adapter_runs_once(tmp_path, monkeypatch):
    recorder, _, logs = enabled(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise OSError("fixture error whose contents must not be exposed")
    monkeypatch.setattr(recorder, "_write", fail)
    adapter = Adapter()
    result = recorder.complete(adapter, messages())
    assert len(adapter.calls) == 1 and result.status == "json_invalid"
    assert result.acceptance_trace["status"] == "write_failed" and result.acceptance_trace["reason"] == "OSError"
    assert not list((logs / "acceptance_model_io").rglob("*.json"))


def test_partial_write_and_failed_cleanup_cannot_exceed_total_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_module, "TOTAL_LIMIT", 64 * 1024)
    monkeypatch.setattr(trace_module, "RESPONSE_LIMIT", 32 * 1024)
    monkeypatch.setattr(trace_module, "METADATA_LIMIT", 1024)
    recorder, _, logs = enabled(tmp_path, monkeypatch, max_calls=4)
    original_open, original_unlink = Path.open, Path.unlink
    class PartialWrite:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def write(self, content):
            self.stream.write(content[:len(content) // 2])
            raise OSError("simulated partial write")
    def partial_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return PartialWrite(stream) if path.name == "response.txt" and mode == "xb" else stream
    def failed_unlink(path, *args, **kwargs):
        if path.name == "response.txt":
            raise OSError("simulated failed cleanup")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", partial_open)
    monkeypatch.setattr(Path, "unlink", failed_unlink)
    adapter = Adapter("x" * (32 * 1024))
    results = [recorder.complete(adapter, messages()) for _ in range(4)]
    assert len(adapter.calls) == 4 and all(r.raw_text == adapter.raw for r in results)
    assert all(r.acceptance_trace["status"] == "write_failed" for r in results)
    assert sum(p.stat().st_size for p in (logs / "acceptance_model_io").rglob("*") if p.is_file()) <= 64 * 1024


def test_existing_trace_is_never_reused_or_overwritten(tmp_path, monkeypatch):
    recorder, repo, logs = enabled(tmp_path, monkeypatch)
    recorder.complete(Adapter(), messages())
    before = {p: p.read_bytes() for p in (logs / "acceptance_model_io").rglob("*") if p.is_file()}
    second = AcceptanceTrace.from_environment("run_fixture", 8, repo_root=repo)
    result = second.complete(Adapter(), messages())
    assert result.acceptance_trace["reason"] == "run_trace_already_exists"
    assert {p: p.read_bytes() for p in (logs / "acceptance_model_io").rglob("*") if p.is_file()} == before


def test_model_exception_is_propagated_and_no_extra_attempt_is_made(tmp_path, monkeypatch):
    recorder, _, _ = enabled(tmp_path, monkeypatch)
    calls = []
    def raises(request):
        calls.append(request)
        raise RuntimeError("original model exception")
    with pytest.raises(RuntimeError, match="original model exception"):
        recorder.complete(SimpleNamespace(complete=raises), messages())
    assert len(calls) == 1


def test_split_materials_and_model_gateway_time_budgets_are_unchanged(tmp_path, monkeypatch):
    recorder, _, logs = enabled(tmp_path, monkeypatch, max_calls=2)
    budget = RunBudget(limits={"max_gateway_requests": 2, "max_run_seconds": 600}, clock=lambda: 123.0)
    adapter = Adapter(budget=budget)
    planner = ModelCallPlanner(MICConfig(raw={}), SimpleNamespace(get=lambda _: adapter), CallBudget(max_model_calls_per_run=2))
    planner.acceptance_trace = recorder
    profile = TargetProfile(target_id="test", canonical_name="测试主体", type="company")
    chunks = [[Passage(passage_id="p1", section="正文", text="first chunk")],
              [Passage(passage_id="p2", section="正文", text="second chunk")]]
    before_deadline, before_started = budget.deadline_at, budget.started_at
    results = planner._run_split(profile, {"source_link_id": "link_split", "title": "两段"}, chunks,
                                 {"models": [{"model_id": "fixture"}]})
    assert len(adapter.calls) == planner.budget.calls_used == budget.used["gateway_requests_sent"] == 2
    assert budget.deadline_at == before_deadline and budget.started_at == before_started
    for expected, result in zip(chunks, results):
        material = json.loads((directory(logs, result) / "input.json").read_text())["user_material"][0]
        assert material["selected_passages"] == [p.model_dump() for p in expected]
        assert material["source_metadata"]["source_link_id"] == "link_split"
    assert all(result.status == "json_invalid" and result.parsed is None for result in results)


def test_triage_budget_and_actual_publication_metadata_are_material_not_config(tmp_path, monkeypatch):
    recorder, _, logs = enabled(tmp_path, monkeypatch)
    request = messages()
    payload = json.loads(request[1]["content"])
    budget = {"scope": "entire_task_run", "snapshot": "before_this_triage_call",
              "successful_read_basis": "physical_fetch_including_duplicates",
              "limits": {"max_links_to_read": 4, "max_model_calls": 8},
              "remaining": {"model_calls": 7, "links_read": 4}, "batch_index": 1,
              "candidate_count": 20, "batch_size_limit": 20}
    payload["reading_budget"] = {**budget, "config": "never-copy-deployment-settings"}
    payload["source_metadata"].update(publish_time=None,
                                      publication_time={"status": "unknown", "published_at": None})
    request[1]["content"] = json.dumps(payload, ensure_ascii=False)
    result = recorder.complete(Adapter(), request)
    material = json.loads((directory(logs, result) / "input.json").read_text())["user_material"][0]
    assert material["reading_budget"] == budget
    assert material["source_metadata"]["publish_time"] is None
    assert material["source_metadata"]["publication_time"] == {"status": "unknown", "published_at": None}


@pytest.fixture
def navigation_material():
    scope = {"kind": "index", "parser_version": "index_v1", "coverage": "partial",
             "visibility": "visible", "eligible_entries": 8, "returned_entries": 4,
             "selected_entries": 2, "truncated": True, "truncation_reasons": ["selected_passage_limit"],
             "ordering": "source_order", "completeness": "unknown", "pagination_detected": True}
    provenance = {"protocol": "index_navigation_v1", "kind": "entry", "entry_id": "entry_02",
                  "entry_text": "第二条公告原文。", "link_index": 1,
                  "parent_source_link_id": "link_parent", "parent_url": "https://example.invalid/index",
                  "link_text": "公告正文", "original_url": "https://example.invalid/files/report.pdf",
                  "scope": scope}
    context = {"protocol": "index_navigation_v1", "parent_source_link_id": "link_parent",
               "parent_url": "https://example.invalid/index",
               "material_scope_summary": {key: scope[key] for key in
                   ("selected_entries", "truncated", "truncation_reasons", "ordering", "completeness", "coverage")},
               "read_state": {"attempted_urls": ["https://example.invalid/index"], "successful_reads": 1}}
    return provenance, context


@pytest.mark.parametrize("location", ["items", "source_metadata"])
@pytest.mark.parametrize("kind", ["entry", "pagination"])
def test_navigation_exact_binding_and_context_are_saved_without_extra_calls(
        tmp_path, monkeypatch, navigation_material, location, kind):
    recorder, _, logs = enabled(tmp_path, monkeypatch)
    provenance, context = navigation_material
    if kind == "pagination":
        provenance.update(kind="pagination", entry_id=None, entry_text="", link_index=0,
                          link_text="下一页", original_url="https://example.invalid/index?page=2")
    else:
        # Retain the complete bound entry beyond the former 8k character input
        # default; the trace still uses its existing UTF-8 byte limit.
        provenance["entry_text"] = "目录条目原文。" * 1300
    request = messages()
    payload = json.loads(request[1]["content"])
    payload["navigation_context"] = context
    if location == "items":
        payload["task"] = "serp_batch_triage"
        payload["items"] = [{"id": "link_child", "title": provenance["link_text"],
                             "snippet": provenance["entry_text"], "url": provenance["original_url"],
                             "domain": "example.invalid", "source_type": "unknown", "read_gate": {},
                             "rule_priority": 0, "navigation": provenance}]
    else:
        payload["source_metadata"].update(source_link_id="link_child", url=provenance["original_url"],
                                           navigation=provenance)
    request[1]["content"] = json.dumps(payload, ensure_ascii=False)
    before = deepcopy(request)
    assert len(request[1]["content"]) < 32000
    budget = RunBudget(limits={"max_gateway_requests": 8, "max_run_seconds": 600}, clock=lambda: 123.0)
    adapter = Adapter(budget=budget)
    planner = ModelCallPlanner(MICConfig(raw={}), SimpleNamespace(get=lambda _: adapter),
                               CallBudget(max_model_calls_per_run=8))
    planner.acceptance_trace = recorder
    deadline = budget.deadline_at
    result, = planner._dispatch("single_model", {"models": [{"model_id": "fixture"}]}, request)
    saved = json.loads((directory(logs, result) / "input.json").read_text())["user_material"][0]
    bound = saved["items"][0] if location == "items" else saved["source_metadata"]
    assert bound["navigation"] == provenance
    assert bound["url"] == provenance["original_url"]
    assert bound["id" if location == "items" else "source_link_id"] == "link_child"
    assert saved["navigation_context"] == context
    assert request == before and adapter.calls == [before] and result.raw_text == adapter.raw
    assert planner.budget.calls_used == budget.used["gateway_requests_sent"] == recorder.calls == 1
    assert budget.deadline_at == deadline
    assert not result.acceptance_trace["input_redacted"]
    metadata = json.loads((directory(logs, result) / "request.json").read_text())
    assert metadata["limits"] == {"input_bytes": 256 * 1024, "response_bytes": 2 * 1024 * 1024,
                                  "run_bytes": 32 * 1024 * 1024, "calls": 8}


def test_navigation_whitelists_and_recursive_redaction_exclude_nested_secrets(
        tmp_path, monkeypatch, navigation_material):
    recorder, _, logs = enabled(tmp_path, monkeypatch)
    provenance, context = navigation_material
    request = messages()
    monkeypatch.setenv("TRACE_PRIVATE_TOKEN", "navigation-private-env-value")
    provenance.update(entry_text="公告 " + Adapter.api_key + " " + request[0]["content"],
                      config={"endpoint": "navigation-config-value"},
                      unexpected_material="navigation-unknown-value")
    provenance["scope"].update(
        configuration={"model": "navigation-scope-config-value"},
        ordering={"description": "原始顺序 navigation-private-env-value",
                  "headers": {"Cookie": "navigation-nested-cookie-value"},
                  "secret": "navigation-nested-secret-value"},
        unexpected_material="navigation-scope-unknown-value")
    context["material_scope_summary"].update(
        system_prompt="navigation-summary-prompt-value",
        unexpected_material="navigation-summary-unknown-value")
    context["read_state"].update(
        attempted_urls=["https://example.invalid/index?access_token=navigation-url-token-value&visible=1"],
        config={"endpoint": "navigation-state-config-value"},
        unexpected_material="navigation-state-unknown-value")
    context.update(configuration={"model": "navigation-context-config-value"},
                   unexpected_material="navigation-context-unknown-value")
    payload = json.loads(request[1]["content"])
    payload["navigation_context"] = context
    payload["source_metadata"]["navigation"] = provenance
    payload["items"] = [{"id": "link_child", "navigation": provenance}]
    request[1]["content"] = json.dumps(payload, ensure_ascii=False)
    before = deepcopy(request)
    adapter = Adapter()
    result = recorder.complete(adapter, request)
    saved = (directory(logs, result) / "input.json").read_text()
    for secret in (Adapter.api_key, request[0]["content"], "navigation-private-env-value",
                   "navigation-config-value", "navigation-scope-config-value", "navigation-nested-cookie-value",
                   "navigation-nested-secret-value", "navigation-summary-prompt-value", "navigation-url-token-value",
                   "navigation-state-config-value", "navigation-context-config-value", "navigation-unknown-value",
                   "navigation-scope-unknown-value", "navigation-summary-unknown-value",
                   "navigation-state-unknown-value", "navigation-context-unknown-value"):
        assert secret not in saved
    material = json.loads(saved)["user_material"][0]
    assert material["source_metadata"]["navigation"] == material["items"][0]["navigation"]
    assert material["source_metadata"]["navigation"]["entry_text"].startswith("公告 [REDACTED]")
    assert material["source_metadata"]["navigation"]["scope"]["ordering"] == {
        "description": "原始顺序 [REDACTED]"}
    assert "visible=1" in material["navigation_context"]["read_state"]["attempted_urls"][0]
    assert result.acceptance_trace["input_redacted"]
    assert request == before and adapter.calls == [before] and result.raw_text == adapter.raw


def test_navigation_material_obeys_existing_input_limit_without_partial_binding(
        tmp_path, monkeypatch, navigation_material):
    recorder, _, logs = enabled(tmp_path, monkeypatch)
    provenance, context = navigation_material
    provenance["entry_text"] = "汉" * trace_module.INPUT_LIMIT
    request = messages()
    payload = json.loads(request[1]["content"])
    payload["navigation_context"] = context
    payload["source_metadata"]["navigation"] = provenance
    request[1]["content"] = json.dumps(payload, ensure_ascii=False)
    before = deepcopy(request)
    adapter = Adapter()
    result = recorder.complete(adapter, request)
    folder = directory(logs, result)
    assert result.acceptance_trace["input_status"] == "omitted_input_limit"
    assert not (folder / "input.json").exists() and (folder / "response.txt").is_file()
    assert request == before and adapter.calls == [before] and result.raw_text == adapter.raw


@pytest.mark.parametrize("mode", ["single_model", "priority_fallback", "parallel_ensemble", "cascade"])
def test_existing_call_modes_trace_each_existing_call_once(tmp_path, monkeypatch, mode):
    recorder, _, logs = enabled(tmp_path, monkeypatch)
    adapter = Adapter()
    planner = ModelCallPlanner(MICConfig(raw={}), SimpleNamespace(get=lambda _: adapter), CallBudget())
    planner.acceptance_trace = recorder
    result, = planner._dispatch(mode, {"models": [{"model_id": "fixture"}]}, messages())
    assert len(adapter.calls) == planner.budget.calls_used == 1
    assert (directory(logs, result) / "response.txt").read_text() == adapter.raw


def test_trace_reference_flows_through_existing_model_run_and_report_diagnostics(tmp_path, monkeypatch):
    from mic.pipeline import Pipeline, RunStats
    from mic.modeling.call_planner import LinkModelResult

    recorder, _, _ = enabled(tmp_path, monkeypatch)
    result = recorder.complete(Adapter(), messages())
    saved_runs, saved_outputs = [], []
    def save_run(value):
        saved_runs.append(value)
        return "mrun_fixture"
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = MICConfig(raw={})
    pipeline.policy_version = "fixture"
    pipeline.repo = SimpleNamespace(save_model_run=save_run, save_model_output=saved_outputs.append)
    stats = RunStats()
    link = LinkModelResult("link_a", "bundle_extraction", "single_model", outputs=[result])
    assert pipeline._persist_model_outputs("link_a", link, SimpleNamespace(passages=[]), stats) == []
    assert saved_runs[0]["request_diagnostics"]["acceptance_trace"] == result.acceptance_trace
    assert stats.model_requests[0]["model_run_id"] == "mrun_fixture"
    assert stats.model_requests[0]["acceptance_trace"] == result.acceptance_trace
    assert saved_outputs[0]["output_json"] is None and saved_outputs[0]["schema_valid"] is False
    assert all("raw_text" not in row for row in saved_runs + saved_outputs)
