"""Opt-in, bounded acceptance evidence; never a parser, retry or model policy.

Only material from actual user messages and redacted response text is written.
The ordinary log, database and published business records never receive bodies.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from mic.modeling.json_response import ERROR_KINDS

INPUT_LIMIT = 256 * 1024
RESPONSE_LIMIT = 2 * 1024 * 1024
TOTAL_LIMIT = 32 * 1024 * 1024
CALL_LIMIT = 16
METADATA_LIMIT = 8 * 1024  # two metadata files per call, included in TOTAL_LIMIT
_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_SECRET = re.compile(r"(?i)(?:^|[_-])(?:api[_-]?key|key|token|secret|password|passwd|cookie|cookies|authorization|credential)(?:$|[_-])")
_SOURCE_KEYS = {"source_link_id", "title", "url", "source_name", "source_type", "publish_time",
                "publication_time", "collection_time_window", "query_family", "material_scope",
                "event_resolution_context"}
_CONTEXT_KEYS = {"protocol", "questions", "reference_time", "time_window"}
_READING_BUDGET_KEYS = {"scope", "snapshot", "successful_read_basis", "limits", "remaining",
                        "batch_index", "candidate_count", "batch_size_limit"}
_ITEM_KEYS = {"id", "title", "snippet", "url", "domain", "source_type", "read_gate", "rule_priority"}
_NAVIGATION_KEYS = {"protocol", "kind", "entry_id", "entry_text", "link_index",
                    "parent_source_link_id", "parent_url", "link_text", "original_url"}
_NAVIGATION_SCOPE_KEYS = {"kind", "parser_version", "coverage", "visibility", "eligible_entries",
                          "returned_entries", "selected_entries", "truncated", "truncation_reasons",
                          "ordering", "completeness", "pagination_detected"}
_NAVIGATION_CONTEXT_KEYS = {"protocol", "parent_source_link_id", "parent_url"}
_NAVIGATION_SUMMARY_KEYS = {"selected_entries", "truncated", "truncation_reasons",
                            "ordering", "completeness", "coverage"}
_NAVIGATION_READ_STATE_KEYS = {"attempted_urls", "successful_reads"}
_DENIED = {"system", "system_prompt", "messages", "config", "configuration", "headers",
           "output_limits", "output_schema_hint", "model_registry", "api_key", "cookie"}


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class _TraceFailure(Exception):
    """Only fixed recorder reason codes may be exposed in diagnostics."""


def _failure_reason(exc):
    return str(exc) if isinstance(exc, _TraceFailure) else type(exc).__name__


def _repo_root():
    for path in Path(__file__).resolve().parents:
        if (path / "tools/market_intelligence_collector/mic").is_dir() and (path / "agents").is_dir():
            return path
    return None


def _under_component_logs(path, repo):
    try:
        parts = path.relative_to(repo).parts
    except ValueError:
        return False
    return len(parts) >= 3 and parts[0] in {"agents", "tools"} and parts[2] == "logs"


class _Redactor:
    def __init__(self, messages, api_key):
        values = [api_key] if isinstance(api_key, str) and api_key else []
        values.extend(value for key, value in os.environ.items() if _SECRET.search(key) and value)
        for message in messages:
            if message.get("role") == "system" and isinstance(message.get("content"), str):
                prompt = message["content"]
                values.append(prompt)
                values.extend(line for line in prompt.splitlines() if len(line.strip()) >= 8)
        # JSON escaping must not hide a known secret or a system-prompt echo.
        values.extend(json.dumps(value, ensure_ascii=False)[1:-1] for value in list(values))
        values.extend(json.dumps(value, ensure_ascii=True)[1:-1] for value in list(values))
        self.values = sorted({value for value in values if value}, key=len, reverse=True)
        self.changed = False

    def text(self, value):
        original = value
        for secret in self.values:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(\bhttps?://)[^/\s?#@]+@", r"\1[REDACTED]@", value)
        value = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?im)(\b(?:set-cookie|cookie|authorization)\s*:\s*)[^\r\n]*",
                       r"\1[REDACTED]", value)
        names = r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|cookie|authorization|credential|signature|sig|key)"
        value = re.sub(r"(?i)([?&]" + names + r"=)[^&\s\"']+", r"\1[REDACTED]", value)
        assignment = (r"(?i)(\b" + names + r"\b[\"']?\s*[:=]\s*)"
                      r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}&\]]+)''')
        value = re.sub(assignment, lambda match: match[1] + '"[REDACTED]"', value)
        self.changed |= value != original
        return value

    def value(self, value, depth=0):
        if depth > 32:
            self.changed = True
            return "[OMITTED_DEPTH_LIMIT]"
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if str(key).lower().replace("-", "_") in _DENIED or _SECRET.search(str(key)):
                    self.changed = True
                    continue
                result[self.text(str(key))] = self.value(item, depth + 1)
            return result
        if isinstance(value, list):
            return [self.value(item, depth + 1) for item in value]
        return self.text(value) if isinstance(value, str) else value


def _navigation_material(value):
    selected = {key: item for key, item in value.items() if key in _NAVIGATION_KEYS}
    if isinstance(value.get("scope"), dict):
        selected["scope"] = {key: item for key, item in value["scope"].items()
                             if key in _NAVIGATION_SCOPE_KEYS}
    return selected


def _navigation_context_material(value):
    selected = {key: item for key, item in value.items() if key in _NAVIGATION_CONTEXT_KEYS}
    for key, allowed in (("material_scope_summary", _NAVIGATION_SUMMARY_KEYS),
                         ("read_state", _NAVIGATION_READ_STATE_KEYS)):
        if isinstance(value.get(key), dict):
            selected[key] = {name: item for name, item in value[key].items() if name in allowed}
    return selected


def _material(messages, redactor):
    material = []
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        content = message["content"]
        # A large or non-JSON user message is not dumped via a permissive fallback.
        if len(content.encode("utf-8")) > 4 * INPUT_LIMIT:
            return None, "omitted_input_limit"
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            return None, "unsupported_user_material"
        if not isinstance(payload, dict):
            return None, "unsupported_user_material"
        selected = {}
        for key in ("task", "selected_passages", "field_conflicts"):
            if key in payload:
                selected[key] = payload[key]
        for key, allowed in (("source_metadata", _SOURCE_KEYS), ("task_context", _CONTEXT_KEYS),
                             ("reading_budget", _READING_BUDGET_KEYS)):
            if isinstance(payload.get(key), dict):
                selected[key] = {name: value for name, value in payload[key].items() if name in allowed}
        source = payload.get("source_metadata")
        if isinstance(source, dict) and isinstance(source.get("navigation"), dict):
            selected["source_metadata"]["navigation"] = _navigation_material(source["navigation"])
        if isinstance(payload.get("navigation_context"), dict):
            selected["navigation_context"] = _navigation_context_material(payload["navigation_context"])
        if isinstance(payload.get("items"), list):
            selected["items"] = []
            for item in payload["items"]:
                if not isinstance(item, dict):
                    continue
                candidate = {key: value for key, value in item.items() if key in _ITEM_KEYS}
                if isinstance(item.get("navigation"), dict):
                    candidate["navigation"] = _navigation_material(item["navigation"])
                selected["items"].append(candidate)
        material.append(redactor.value(selected))
    if not material:
        return None, "unsupported_user_material"
    encoded = _bytes({"user_material": material})
    return (encoded, "saved") if len(encoded) <= INPUT_LIMIT else (None, "omitted_input_limit")


class AcceptanceTrace:
    """A per-run recorder. Its limits never alter the existing model budget."""
    def __init__(self, *, reason, run_id, max_calls, log_dir=None):
        self.reason = reason
        self.run_id = run_id
        self.max_calls = max_calls
        self.log_dir = log_dir
        self.enabled = reason == "enabled"
        self.calls = 0
        self.total_bytes = 0

    @classmethod
    def from_environment(cls, run_id, max_calls, *, repo_root=None):
        def disabled(reason):
            return cls(reason=reason, run_id=run_id, max_calls=0)
        if os.environ.get("MIC_ACCEPTANCE_MODEL_IO") != "1":
            return disabled("flag_not_enabled")
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            return disabled("invalid_run_id")
        if type(max_calls) is not int or max_calls <= 0:
            return disabled("invalid_model_call_limit")
        configured = os.environ.get("MIC_LOG_DIR")
        if not configured or not Path(configured).is_absolute():
            return disabled("explicit_absolute_log_dir_required")
        try:
            repo = Path(repo_root).resolve() if repo_root is not None else _repo_root()
            directory = Path(configured).resolve()
            if repo is None or not _under_component_logs(directory, repo):
                return disabled("outside_component_logs")
            active_value = os.environ.get("MIC_ACTIVE_LOG_FILE", "")
            active = Path(active_value)
            if (not active.is_absolute() or not active.is_file()
                    or active.resolve().parent != directory or active.name != run_id + ".log"):
                return disabled("active_run_log_mismatch")
            result = cls(reason="enabled", run_id=run_id, max_calls=min(max_calls, CALL_LIMIT), log_dir=directory)
            run_dir = directory / "acceptance_model_io" / run_id
            if run_dir.exists():
                # Never append to a previous recorder's quota or overwrite evidence.
                return disabled("run_trace_already_exists")
            return result
        except (OSError, RuntimeError):
            return disabled("log_path_unavailable")

    def _write(self, path, content, *, metadata=False):
        limit = TOTAL_LIMIT if metadata else TOTAL_LIMIT - self.max_calls * 2 * METADATA_LIMIT
        if metadata and len(content) > METADATA_LIMIT:
            raise _TraceFailure("trace_metadata_limit")
        if self.total_bytes + len(content) > limit:
            raise _TraceFailure("trace_total_limit")
        if not path.parent.resolve().is_relative_to(self.log_dir):
            raise _TraceFailure("trace_path_outside_logs")
        # Reserve before I/O: a partial write may survive failed cleanup. Keeping
        # that reservation bounds physical files even when the filesystem fails.
        self.total_bytes += len(content)
        created = False
        try:
            with path.open("xb") as stream:
                created = True
                stream.write(content)
        except Exception:
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise

    def _start(self, adapter, messages):
        info = {"enabled": self.enabled, "reason": self.reason}
        if not self.enabled:
            return info, None, None
        if self.calls >= self.max_calls:
            info.update(status="not_saved", reason="trace_call_limit")
            return info, None, None
        self.calls += 1
        trace_id = f"call_{self.calls:02d}_{uuid4().hex}"
        relative = Path("acceptance_model_io") / self.run_id / trace_id
        info.update(trace_id=trace_id, relative_path=relative.as_posix(), status="started")
        directory = self.log_dir / relative
        try:
            if not directory.resolve().is_relative_to(self.log_dir):
                raise _TraceFailure("trace_path_outside_logs")
            directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            redactor = _Redactor(messages, getattr(adapter, "api_key", ""))
            material, status = _material(messages, redactor)
            info.update(input_status=status, input_redacted=redactor.changed)
            if material is not None:
                self._write(directory / "input.json", material)
                info["input_sha256"] = sha256(material).hexdigest()
            request = {"trace_id": trace_id, "run_id": self.run_id,
                       "input_status": status, "input_redacted": redactor.changed,
                       "system_prompt_saved": False, "configuration_saved": False,
                       "limits": {"input_bytes": INPUT_LIMIT, "response_bytes": RESPONSE_LIMIT,
                                  "run_bytes": TOTAL_LIMIT, "calls": self.max_calls}}
            self._write(directory / "request.json", _bytes(request), metadata=True)
            return info, directory, redactor
        except Exception as exc:
            info.update(status="write_failed", reason=_failure_reason(exc))
            return info, None, None

    def _finish(self, result, info, directory, redactor):
        if directory is None:
            return
        try:
            raw = result.raw_text
            original = raw.encode("utf-8")
            redactor.changed = False
            saved = redactor.text(raw).encode("utf-8")
            details = {"raw_sha256": sha256(original).hexdigest(), "raw_bytes": len(original),
                       "redacted_bytes": len(saved), "redacted": redactor.changed,
                       "redacted_sha256": sha256(saved).hexdigest(),
                       "complete": len(saved) <= RESPONSE_LIMIT}
            if len(saved) <= RESPONSE_LIMIT:
                self._write(directory / "response.txt", saved)
                details["saved_sha256"] = sha256(saved).hexdigest()
            else:
                # Separate, labelled segments cannot be mistaken for one complete JSON.
                head = saved[:RESPONSE_LIMIT // 2].decode("utf-8", errors="ignore").encode("utf-8")
                tail = saved[-RESPONSE_LIMIT // 2:].decode("utf-8", errors="ignore").encode("utf-8")
                self._write(directory / "response.head.txt", head)
                self._write(directory / "response.tail.txt", tail)
                details.update(head_end_byte=len(head), tail_start_byte=len(saved) - len(tail),
                               omitted_bytes=len(saved) - len(head) - len(tail),
                               head_sha256=sha256(head).hexdigest(), tail_sha256=sha256(tail).hexdigest())
            info.update(status="saved", response_complete=details["complete"], response_redacted=details["redacted"])
            metadata = {"trace_id": info["trace_id"], "run_id": self.run_id,
                        "status": result.status, "finish_reason": result.finish_reason,
                        "requested_max_tokens": result.requested_max_tokens,
                        "output_tokens": result.output_tokens, "provider_request_id": result.provider_request_id,
                        "response": details}
            # Fixed, content-free parse diagnostics only; do not copy arbitrary
            # error messages or model text into this bounded metadata record.
            parse_error = getattr(result, "json_parse_error", None)
            if (isinstance(parse_error, dict) and isinstance(parse_error.get("kind"), str)
                    and parse_error["kind"] in ERROR_KINDS):
                metadata["json_parse_error"] = {"kind": parse_error["kind"]}
                for key in ("offset", "line", "column"):
                    value = parse_error.get(key)
                    if type(value) is int and 0 <= value <= len(raw) + 1:
                        metadata["json_parse_error"][key] = value
                if type(parse_error.get("at_end")) is bool:
                    metadata["json_parse_error"]["at_end"] = parse_error["at_end"]
            recovery = getattr(result, "json_recovery", None)
            if (isinstance(recovery, dict)
                    and recovery.get("strategy") == "reextract_original_input"
                    and type(recovery.get("attempt")) is int and recovery["attempt"] == 1
                    and isinstance(recovery.get("parent_response_sha256"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", recovery["parent_response_sha256"])):
                safe = {key: recovery[key] for key in (
                    "strategy", "attempt", "parent_response_sha256")}
                index = recovery.get("parent_output_index")
                if type(index) is int and 0 <= index < CALL_LIMIT:
                    safe["parent_output_index"] = index
                for key in ("parent_trace_id", "parent_provider_request_id"):
                    value = recovery.get(key)
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
                        safe[key] = value
                metadata["json_recovery"] = safe
            self._write(directory / "result.json", _bytes(redactor.value(metadata)), metadata=True)
        except Exception as exc:
            info.update(status="write_failed", reason=_failure_reason(exc))

    def complete(self, adapter, messages, *, json_recovery=None):
        # Exceptions from diagnostics are contained; model exceptions are not swallowed.
        if self.reason == "flag_not_enabled":
            result = adapter.complete(messages)
            if json_recovery is not None:
                result.json_recovery = dict(json_recovery)
            return result
        info, directory, redactor = self._start(adapter, messages)
        result = adapter.complete(messages)
        if json_recovery is not None:
            result.json_recovery = dict(json_recovery)
        self._finish(result, info, directory, redactor)
        result.acceptance_trace = info
        return result
