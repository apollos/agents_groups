from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_trade_intel.ids import utc_now_iso


@dataclass
class ToolResult:
    tool_name: str
    operation: str
    request: dict[str, Any]
    # Adapters construct the result first and set the final status after the tool call returns.
    status: str = "pending"
    result: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    result_ref: str | None = None
    raw_result_ref: str | None = None
    started_at: str = field(default_factory=utc_now_iso)
    completed_at: str | None = None

    def finish(self) -> "ToolResult":
        self.completed_at = utc_now_iso()
        return self
