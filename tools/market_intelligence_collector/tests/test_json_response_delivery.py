"""Offline SDK-to-trace delivery keeps malformed responses and bounded diagnostics."""
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


def _setup(tmp_path, monkeypatch, raw, finish_reason, *, injected_diagnostic=None):
    # No SDK construction or network call is possible: the only completion
    # implementation is this in-memory fake, used by the real adapter.
    calls = []

    def create(**kwargs):
        calls.append(deepcopy(kwargs))
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=raw), finish_reason=finish_reason)],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=25),
            model="offline-served-model", id="offline-response-id")

    monkeypatch.setattr(adapter_module, "OpenAI", object())
    adapter = ModelAdapter(
        model_config_id="offline", provider="offline",
        provider_type="openai_compatible_direct", endpoint="http://127.0.0.1:1/v1",
        model="offline-requested-model", api_key="offline-sdk-fixture-credential",
        allow_mock=False, max_output_tokens=65536)
    adapter._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    runtime_budget = RunBudget(
        limits={"max_gateway_requests": 1, "max_run_seconds": 600},
        clock=lambda: 123.0)
    adapter.set_budget(runtime_budget)
    if injected_diagnostic is not None:
        original_complete = adapter.complete

        def complete(messages):
            result = original_complete(messages)
            result.json_parse_error = deepcopy(injected_diagnostic)
            result.error_message = "private-diagnostic-message-must-not-be-captured"
            return result

        monkeypatch.setattr(adapter, "complete", complete)

    repo = tmp_path / "repo"
    logs = repo / "tools/market_intelligence_collector/logs/acceptance/json_delivery"
    logs.mkdir(parents=True)
    active = logs / "run_json_delivery.log"
    active.write_text("")
    monkeypatch.setenv("MIC_ACCEPTANCE_MODEL_IO", "1")
    monkeypatch.setenv("MIC_LOG_DIR", str(logs))
    monkeypatch.setenv("MIC_ACTIVE_LOG_FILE", str(active))
    recorder = AcceptanceTrace.from_environment("run_json_delivery", 8, repo_root=repo)
    planner = ModelCallPlanner(
        MICConfig(raw={}), SimpleNamespace(get=lambda _: adapter),
        CallBudget(max_model_calls_per_run=1))
    planner.acceptance_trace = recorder
    request = [{
        "role": "user",
        "content": json.dumps({
            "task": "bundle_extraction",
            "source_metadata": {"source_link_id": "link_offline"},
            "selected_passages": [
                {"passage_id": "p0", "section": "正文", "text": "供格式回归使用的正文。"}
            ],
        }, ensure_ascii=False),
    }]
    result, = planner._dispatch(
        "single_model", {"models": [{"model_id": "offline"}]}, request)
    directory = logs / result.acceptance_trace["relative_path"]
    return SimpleNamespace(
        result=result, directory=directory, recorder=recorder, planner=planner,
        runtime_budget=runtime_budget, calls=calls, request=request)


@pytest.mark.parametrize("raw,finish_reason,status,error_kind", [
    (' \n{"说明":"尚未闭合"', "stop", "json_invalid", "syntax_error"),
    ('{"value":1,"value":2}', "stop", "json_invalid", "duplicate_key"),
    ('{"value":1}', "stop", "success", None),
    ('{"value":1}', "length", "output_truncated", None),
])
def test_real_adapter_trace_retains_raw_response_and_does_not_retry(
        tmp_path, monkeypatch, raw, finish_reason, status, error_kind):
    run = _setup(tmp_path, monkeypatch, raw, finish_reason)
    result = run.result
    metadata = json.loads((run.directory / "result.json").read_text())
    request_metadata = json.loads((run.directory / "request.json").read_text())
    raw_bytes = raw.encode("utf-8")

    assert result.status == metadata["status"] == status
    assert result.raw_text == raw
    assert (run.directory / "response.txt").read_bytes() == raw_bytes
    assert metadata["response"] == {
        "raw_sha256": sha256(raw_bytes).hexdigest(),
        "raw_bytes": len(raw_bytes),
        "redacted_bytes": len(raw_bytes),
        "redacted": False,
        "redacted_sha256": sha256(raw_bytes).hexdigest(),
        "complete": True,
        "saved_sha256": sha256(raw_bytes).hexdigest(),
    }
    if error_kind is not None:
        assert result.parsed is None
        assert result.json_parse_error["kind"] == error_kind
        assert metadata["json_parse_error"] == result.json_parse_error
        assert set(metadata["json_parse_error"]) <= {
            "kind", "offset", "line", "column", "at_end"}
        if error_kind == "syntax_error":
            # Positions are in the unchanged original Unicode response.
            assert metadata["json_parse_error"] == {
                "kind": "syntax_error", "offset": len(raw), "line": 2,
                "column": len(raw.split("\n")[1]) + 1, "at_end": True}
    else:
        assert "json_parse_error" not in metadata
        assert result.json_parse_error is None
        assert result.parsed == ({"value": 1} if status == "success" else None)
    assert "error_message" not in metadata
    assert metadata["requested_max_tokens"] == 65536
    assert metadata["finish_reason"] == finish_reason
    assert request_metadata["limits"] == {
        "input_bytes": 256 * 1024, "response_bytes": 2 * 1024 * 1024,
        "run_bytes": 32 * 1024 * 1024, "calls": 8}
    assert len(run.calls) == run.planner.budget.calls_used == run.recorder.calls == 1
    assert run.runtime_budget.used["gateway_requests_sent"] == 1
    assert run.calls[0]["messages"] == run.request
    assert run.calls[0]["max_tokens"] == 65536
    assert run.calls[0]["response_format"] == {"type": "json_object"}
    assert len(list(run.directory.parent.iterdir())) == 1


@pytest.mark.parametrize("unknown_kind", [False, True])
def test_trace_diagnostic_whitelist_rejects_text_and_invalid_positions(
        tmp_path, monkeypatch, unknown_kind):
    raw = '{"value":'
    private = "private-diagnostic-value-must-not-be-captured"
    diagnostic = {
        "kind": private if unknown_kind else "syntax_error",
        "offset": -1, "line": True, "column": len(raw) + 2, "at_end": "yes",
        "raw_text": private, "error_message": private, "extra": private,
    }
    run = _setup(
        tmp_path, monkeypatch, raw, "stop", injected_diagnostic=diagnostic)
    encoded_metadata = (run.directory / "result.json").read_text()
    metadata = json.loads(encoded_metadata)
    assert run.result.json_parse_error == diagnostic  # Recording changes no result.
    assert run.result.raw_text == (run.directory / "response.txt").read_text() == raw
    if unknown_kind:
        assert "json_parse_error" not in metadata
    else:
        assert metadata["json_parse_error"] == {"kind": "syntax_error"}
    assert private not in encoded_metadata
    assert "private-diagnostic-message-must-not-be-captured" not in encoded_metadata
    assert "error_message" not in metadata
    assert len(run.calls) == run.planner.budget.calls_used == run.recorder.calls == 1
    assert run.runtime_budget.used["gateway_requests_sent"] == 1
