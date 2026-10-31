"""Model JSON is one complete object; format failures never become evidence.

These are offline contract tests. The fake transport returns exactly one saved
response and cannot contact a model, search service or browser.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

from mic.modeling import adapter as adapter_module
from mic.modeling.adapter import ModelAdapter
from mic.modeling.json_response import parse_json_object


@pytest.mark.parametrize("wrapper", ["{}", " \n{}\t ", "```json\n{}\n```", "```\n{}\n```"])
def test_complete_object_preserves_values_and_escaped_delimiters(wrapper):
    expected = {
        "text": '中文 {并非结构} [也不是数组] "引号" \\ 路径\n第二行',
        "nested": {"items": [{"value": -3.5}, True, None]},
    }
    raw = wrapper.format(json.dumps(expected, ensure_ascii=False))
    parsed, error = parse_json_object(raw)
    assert parsed == expected
    assert error is None


def test_empty_object_is_a_valid_object_not_a_parse_failure():
    assert parse_json_object("{}") == ({}, None)


@pytest.mark.parametrize("raw", ["[]", '[{"facts":[]}]', '"text"', "1", "1.5", "true", "false", "null"])
def test_complete_non_object_values_are_rejected(raw):
    parsed, error = parse_json_object(raw)
    assert parsed is None
    assert error["kind"] == "non_object"


@pytest.mark.parametrize(
    "raw",
    [
        '[{"facts":[]}',  # Complete inner object inside an incomplete array.
        '{"facts":[]} trailing text',
        'prefix {"facts":[]}',
        '{"facts":[]} {"metrics":[]}',
        '```json\n{"facts":[]}',  # The fence itself must also be complete.
        '```json\n{"facts":[]}\n``` trailing text',
        'prefix\n```json\n{"facts":[]}\n```',
        '```json\n{"facts":[]}\n```\n```json\n{}\n```',
        '{"facts":[],}',
        '{"facts":}',
        '{"value":"unterminated}',
        r'{"value":"invalid \q escape"}',
        "",
        " \n\t ",
    ],
)
def test_no_substring_salvage_or_structural_repair(raw):
    parsed, error = parse_json_object(raw)
    assert parsed is None
    assert error["kind"] == "syntax_error"


def test_missing_outer_brace_reports_eof_without_appending_it():
    # This is the B6 failure shape, with generic content and no source claims.
    raw = '{"facts":[],"content_review":{"claims":[],"bindings":{}}'
    parsed, error = parse_json_object(raw)
    assert parsed is None
    assert error["kind"] == "syntax_error"
    assert error["offset"] == len(raw)
    assert error["line"] == 1
    assert error["column"] == len(raw) + 1
    assert error["at_end"] is True


def test_syntax_error_locations_refer_to_original_fenced_response():
    raw = ' \n```json\n{\n  "value": }\n```\n'
    parsed, error = parse_json_object(raw)
    offset = raw.index("}")
    assert parsed is None
    assert error["kind"] == "syntax_error"
    assert error["offset"] == offset
    assert error["line"] == raw[:offset].count("\n") + 1
    assert error["column"] == offset - raw.rfind("\n", 0, offset)
    assert error["at_end"] is False


@pytest.mark.parametrize(
    "raw",
    [
        '{"facts":[{"value":1}],"facts":[]}',
        '{"review":{"status":"pending","status":"source_supported"}}',
        r'{"facts":[],"\u0066acts":[1]}',
    ],
)
def test_duplicate_keys_never_silently_replace_earlier_values(raw):
    parsed, error = parse_json_object(raw)
    assert parsed is None
    assert error["kind"] == "duplicate_key"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_numeric_constants_are_rejected(constant):
    parsed, error = parse_json_object('{"value":' + constant + "}")
    assert parsed is None
    assert error["kind"] == "non_standard_constant"


@pytest.mark.parametrize("number", ["1e9999", "-1e9999"])
def test_valid_json_numeric_spelling_cannot_overflow_to_infinity(number):
    parsed, error = parse_json_object('{"nested":[{"value":' + number + "}]}")
    assert parsed is None
    assert error["kind"] == "non_finite_number"


def test_extreme_nesting_is_a_contained_format_failure():
    # CPython 3.12's C JSON decoder can accept 2048 levels even with the
    # ordinary Python recursion limit at 1000. Exercise its actual deep-input
    # failure, without inventing a production depth cap from that assumption.
    raw = '{"nested":' + "[" * 10000 + "0" + "]" * 10000 + "}"
    parsed, error = parse_json_object(raw)
    assert parsed is None
    assert error["kind"] == "nesting_limit"


def test_python_integer_decoder_limit_is_a_contained_format_failure():
    get_limit = getattr(sys, "get_int_max_str_digits", None)
    if get_limit is None or get_limit() == 0:
        pytest.skip("This interpreter has no enabled integer conversion limit")
    raw = '{"value":' + "7" * (get_limit() + 1) + "}"
    parsed, error = parse_json_object(raw)
    assert parsed is None
    assert error["kind"] == "decoder_limit"


@pytest.fixture
def adapter_call(monkeypatch):
    # The installed OpenAI SDK is not necessary: _client is always the fake.
    monkeypatch.setattr(adapter_module, "OpenAI", object())

    def call(raw, finish_reason="stop", *, json_mode=None):
        requests, reservations = [], []

        class Completions:
            def create(self, **kwargs):
                requests.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content=raw),
                        finish_reason=finish_reason,
                    )],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
                    model="offline/model",
                    id="response-offline",
                )

        adapter = ModelAdapter(
            model_config_id="offline",
            provider="offline",
            provider_type="openai_compatible",
            endpoint="http://127.0.0.1:1/v1",
            model="offline/model",
            api_key="offline-key",
            allow_mock=False,
            max_output_tokens=65536,
        )
        adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        adapter.set_budget(SimpleNamespace(reserve=reservations.append))
        result = adapter.complete([{"role": "user", "content": "{}"}], json_mode=json_mode)
        return result, requests, reservations

    return call


@pytest.mark.parametrize("raw", [
    '{"facts":[],"content_review":{"claims":[],"bindings":{}}',
    '[{"facts":[]}',
    "[]",
    "true",
    '{"value":NaN}',
    '{"value":1,"value":2}',
])
def test_adapter_records_failure_without_changing_raw_or_sending_again(adapter_call, raw):
    result, requests, reservations = adapter_call(raw)
    assert result.status == result.error_type == "json_invalid"
    assert result.parsed is None
    assert result.raw_text == raw
    assert result.json_parse_error == parse_json_object(raw)[1]
    assert result.request_diagnostics()["json_parse_error"] == result.json_parse_error
    assert result.request_diagnostics()["output_truncated"] is False
    assert result.finish_reason == "stop"
    assert result.error_message
    assert len(requests) == 1
    assert requests[0]["max_tokens"] == result.requested_max_tokens == 65536
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert reservations == ["gateway_requests_sent"]


def test_failure_diagnostics_do_not_copy_model_text_or_duplicate_key_names(adapter_call):
    marker = "sensitive-material-that-must-stay-in-raw-only"
    raw = json.dumps({marker: "value"})[:-1] + "," + json.dumps(marker) + ":false}"
    result, _, _ = adapter_call(raw)
    assert result.raw_text == raw
    assert result.status == "json_invalid"
    assert result.json_parse_error["kind"] == "duplicate_key"
    assert marker not in json.dumps(result.request_diagnostics(), ensure_ascii=False)
    assert marker not in result.error_message


@pytest.mark.parametrize("raw", ['{"facts":[]}', '{"facts":[]'])
def test_length_finish_is_rejected_even_if_json_would_parse(adapter_call, raw):
    result, requests, reservations = adapter_call(raw, "length")
    assert result.status == result.error_type == "output_truncated"
    assert result.parsed is None
    assert result.raw_text == raw
    assert result.json_parse_error is None
    assert "json_parse_error" not in result.request_diagnostics()
    assert result.request_diagnostics()["output_truncated"] is True
    assert len(requests) == 1
    assert reservations == ["gateway_requests_sent"]


def test_success_retains_existing_request_diagnostics_shape(adapter_call):
    result, requests, reservations = adapter_call('{"facts":[]}')
    assert result.status == "success"
    assert result.parsed == {"facts": []}
    assert result.json_parse_error is None
    assert result.error_type is None
    assert "json_parse_error" not in result.request_diagnostics()
    assert len(requests) == 1
    assert reservations == ["gateway_requests_sent"]


def test_plain_text_mode_does_not_apply_json_contract(adapter_call):
    raw = 'transcription with a literal {"incomplete":'
    result, requests, _ = adapter_call(raw, json_mode=False)
    assert result.status == "success"
    assert result.raw_text == raw
    assert result.parsed is None
    assert result.json_parse_error is None
    assert "response_format" not in requests[0]


def test_invalid_response_is_persisted_as_failure_without_validation_or_contribution(adapter_call):
    from mic.config import MICConfig
    from mic.modeling.call_planner import LinkModelResult
    from mic.pipeline import Pipeline, RunStats

    result, _, _ = adapter_call('{"facts":[]')
    saved_runs, saved_outputs = [], []

    def save_run(value):
        saved_runs.append(value)
        return "model-run-offline"

    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = MICConfig(raw={})
    pipeline.policy_version = "offline"
    pipeline.repo = SimpleNamespace(
        save_model_run=save_run,
        save_model_output=saved_outputs.append,
    )
    # No validator is installed: this format failure must not reach it.
    stats = RunStats()
    link = LinkModelResult("link-offline", "bundle_extraction", "single_model", outputs=[result])
    assert pipeline._persist_model_outputs(
        "link-offline", link, SimpleNamespace(passages=[]), stats
    ) == []
    assert saved_runs[0]["error_type"] == "json_invalid"
    assert saved_runs[0]["request_diagnostics"]["json_parse_error"] == result.json_parse_error
    assert stats.model_requests[0]["json_parse_error"] == result.json_parse_error
    assert saved_outputs[0]["output_json"] is None
    assert saved_outputs[0]["schema_valid"] is False
    assert saved_outputs[0]["decision"] is None


def test_parse_diagnostics_survive_repository_explain(adapter_call):
    from mic.store.database import get_database
    from mic.store.repository import Repository

    result, _, _ = adapter_call('{"facts":[]')
    repo = Repository(get_database(os.environ["MIC_DATABASE_URL"]))
    repo.db.create_all()
    run_id = repo.save_model_run({
        "source_link_id": "link-json-format",
        "model_config_id": result.model_config_id,
        "status": result.status,
        "error_type": result.error_type,
        "error_message": result.error_message,
        "request_diagnostics": result.request_diagnostics(),
    })
    (request,) = repo.explain_source_analysis("link-json-format")["model_requests"]
    assert request["model_run_id"] == run_id
    assert request["status"] == request["error_type"] == "json_invalid"
    assert request["json_parse_error"] == result.json_parse_error


def test_parse_success_does_not_authorize_unreviewed_business_records(adapter_call):
    from mic.schemas import Passage
    from mic.validate import BundleValidator

    raw = json.dumps({
        "schema_version": "bundle_extraction_v0.3", "decision": "save_structured",
        "overall_score": 99,
        "confidence": 1,
        "facts": [{"fact_statement": "该公司已经完成一项交易。"}],
        "events": [{"summary": "该公司已经完成一项交易。", "event_date": "2026-08-10"}],
    }, ensure_ascii=False)
    result, _, _ = adapter_call(raw)
    assert result.status == "success"
    passages = [Passage(passage_id="p0", section="正文", text="文中仅说明计划，后续结果尚待确认。")]
    report = BundleValidator({}, require_content_review=True).validate(result.parsed, passages)
    assert not report.schema_valid and report.bundle is None
    assert report.error_code == "response_protocol_invalid"
    assert report.errors == ["content_review: missing"]

    reviewed = {**result.parsed, "content_review": {"protocol": "content_review_v2", "claims": [], "bindings": {}}}
    report = BundleValidator({}, require_content_review=True).validate(reviewed, passages)
    assert report.schema_valid
    assert report.bundle.facts == []
    assert report.bundle.events == []
    assert report.bundle.content_review["held"]
