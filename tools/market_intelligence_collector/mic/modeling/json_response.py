"""Parse one complete JSON object without repairing or salvaging fragments."""
from __future__ import annotations

import json
import math
import re
from typing import Any

_WHITESPACE = " \t\r\n"
_FENCE = re.compile(r"```(?:json)?[ \t]*(?:\r?\n)?(?P<body>.*?)```", re.DOTALL)
ERROR_KINDS = frozenset({
    "syntax_error", "non_object", "duplicate_key", "non_standard_constant",
    "non_finite_number", "nesting_limit", "decoder_limit",
})


class _InvalidJSON(ValueError):
    def __init__(self, kind: str):
        self.kind = kind


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            # Do not disclose model text or silently choose one conflicting value.
            raise _InvalidJSON("duplicate_key")
        result[key] = value
    return result


def _constant(_value: str):
    raise _InvalidJSON("non_standard_constant")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _InvalidJSON("non_finite_number")
    return parsed


def parse_json_object(text: str) -> tuple[dict | None, dict | None]:
    """Return (object, None) or (None, content-free error diagnostics).

    Only a complete response, optionally wrapped in a complete, sole JSON code
    fence, is accepted. Error offsets/line/column refer to the original response
    in characters, not bytes. ``at_end`` refers to the parsed payload, excluding
    JSON whitespace and any valid closing fence. It does not diagnose transport
    truncation. No values, keys or source snippets are copied into diagnostics.
    """
    offset = len(text) - len(text.lstrip(_WHITESPACE))
    payload = text[offset:].rstrip(_WHITESPACE)
    fence = _FENCE.fullmatch(payload)
    if fence is not None:
        offset += fence.start("body")
        payload = fence.group("body")
    try:
        parsed = json.loads(payload, object_pairs_hook=_object,
                            parse_constant=_constant, parse_float=_finite_float)
    except json.JSONDecodeError as exc:
        position = offset + exc.pos
        return None, {
            "kind": "syntax_error",
            "offset": position,
            "line": text.count("\n", 0, position) + 1,
            "column": position - text.rfind("\n", 0, position),
            "at_end": exc.pos >= len(payload.rstrip(_WHITESPACE)),
        }
    except _InvalidJSON as exc:
        return None, {"kind": exc.kind}
    except RecursionError:
        return None, {"kind": "nesting_limit"}
    except ValueError:
        # Python's decoder can reject integers exceeding its configured digit
        # limit. Keep this a parse failure rather than leaking decoder text.
        return None, {"kind": "decoder_limit"}
    if not isinstance(parsed, dict):
        return None, {"kind": "non_object"}
    return parsed, None
