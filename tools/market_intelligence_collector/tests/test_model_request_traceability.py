"""A4: every real request records its output cap, finish_reason and served model;
an output stopped at the cap is an explicit failure, never a complete success."""
from types import SimpleNamespace

from mic.modeling.adapter import ModelAdapter


def _fake_client(content, finish_reason, model="deepseek/deepseek-v4-flash", response_id="chatcmpl-1"):
    class Completions:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50)
            choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
            return SimpleNamespace(choices=[choice], usage=usage, model=model, id=response_id)

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    return client


def _adapter(client):
    adapter = ModelAdapter(model_config_id="openclaw_research", provider="openclaw",
                           provider_type="openclaw_gateway", endpoint="http://127.0.0.1:1/v1",
                           model="openclaw/main", api_key="k", allow_mock=False,
                           max_output_tokens=65536)
    adapter._client = client
    return adapter


def test_successful_request_records_cap_finish_reason_and_served_model():
    client = _fake_client('{"decision": "save_structured"}', "stop")
    res = _adapter(client).complete([{"role": "user", "content": "{}"}])
    assert client.chat.completions.kwargs["max_tokens"] == 65536
    assert res.status == "success" and res.parsed == {"decision": "save_structured"}
    diag = res.request_diagnostics()
    assert diag == {"requested_max_tokens": 65536, "finish_reason": "stop",
                    "served_model": "deepseek/deepseek-v4-flash", "is_mock": False,
                    "output_truncated": False}
    assert res.provider_request_id == "chatcmpl-1"


def test_length_stop_is_output_truncated_not_success_even_if_fragment_parses():
    # A fragment that happens to be valid JSON must still not count as complete.
    client = _fake_client('{"decision": "save_structured", "facts": []}', "length")
    res = _adapter(client).complete([{"role": "user", "content": "{}"}])
    assert res.status == "output_truncated"
    assert res.error_type == "output_truncated"
    assert res.parsed is None
    assert "max_tokens=65536" in res.error_message
    assert res.request_diagnostics()["output_truncated"] is True
    assert res.request_diagnostics()["finish_reason"] == "length"


def test_explicit_max_tokens_override_is_what_gets_recorded():
    client = _fake_client("{}", "stop")
    res = _adapter(client).complete([{"role": "user", "content": "{}"}], max_tokens=262144)
    assert client.chat.completions.kwargs["max_tokens"] == 262144
    assert res.requested_max_tokens == 262144


def test_request_failure_still_records_requested_cap():
    class Completions:
        def create(self, **kwargs):
            raise RuntimeError("gateway 502")

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    res = _adapter(client).complete([{"role": "user", "content": "{}"}])
    assert res.status == "request_failed"
    assert res.requested_max_tokens == 65536 and res.finish_reason is None


def test_model_run_persists_request_diagnostics_and_explain_exposes_them():
    from mic.store.database import get_database
    from mic.store.repository import Repository
    import os

    repo = Repository(get_database(os.environ["MIC_DATABASE_URL"]))
    repo.db.create_all()
    run_id = repo.save_model_run({
        "source_link_id": "link-1", "task_name": "bundle_extraction", "status": "output_truncated",
        "model_config_id": "openclaw_research", "model_name": "openclaw/main",
        "error_type": "output_truncated", "output_tokens": 65536, "provider_request_id": "chatcmpl-9",
        "request_diagnostics": {"requested_max_tokens": 65536, "finish_reason": "length",
                                "served_model": "deepseek/deepseek-v4-flash", "is_mock": False,
                                "output_truncated": True},
    })
    explained = repo.explain_source_analysis("link-1")
    (req,) = explained["model_requests"]
    assert req["model_run_id"] == run_id
    assert req["requested_max_tokens"] == 65536 and req["finish_reason"] == "length"
    assert req["served_model"] == "deepseek/deepseek-v4-flash" and req["output_truncated"] is True
    assert req["provider_request_id"] == "chatcmpl-9"
    # Legacy rows (no diagnostics) read back as unknown, not as a fabricated success.
    repo.save_model_run({"source_link_id": "link-2", "status": "success", "model_config_id": "m"})
    (legacy,) = repo.explain_source_analysis("link-2")["model_requests"]
    assert legacy["finish_reason"] is None and legacy["output_truncated"] is None
