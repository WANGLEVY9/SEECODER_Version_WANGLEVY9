"""Domain types shared by the agent loop, model adapter, and local tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RunState(StrEnum):
    INIT = "init"
    MODEL_REQUEST = "model_request"
    TOOL_DISPATCH = "tool_dispatch"
    FINAL = "final"
    STOP_MAX_STEPS = "stop_max_steps"
    STOP_TOOL_ERROR_LIMIT = "stop_tool_error_limit"
    STOP_CONTEXT_BUDGET = "stop_context_budget"
    FAILED_MODEL = "failed_model"
    FAILED_PROTOCOL = "failed_protocol"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A structured tool request emitted by a model provider."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Provider-neutral representation of a chat message."""

    role: str
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning_content: str | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """One model turn after provider-specific output has been normalized."""

    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    model: str | None = None
    reasoning_content: str | None = None


@dataclass(frozen=True, slots=True)
class ToolError:
    kind: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "message": self.message}


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A serializable tool result. Failures remain observations for the model."""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: ToolError | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(
        cls, data: dict[str, Any] | None = None, *, meta: dict[str, Any] | None = None
    ) -> ToolResult:
        return cls(ok=True, data=data or {}, meta=meta or {})

    @classmethod
    def failure(
        cls, kind: str, message: str, *, data: dict[str, Any] | None = None
    ) -> ToolResult:
        return cls(ok=False, data=data or {}, error=ToolError(kind, message))

    def as_dict(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, "data": self.data, "meta": self.meta}
        return {"ok": False, "data": self.data, "error": self.error.as_dict() if self.error else {}}


@dataclass(frozen=True, slots=True)
class RunOutcome:
    state: RunState
    final_text: str
    steps: int
    trace_path: str | None = None
