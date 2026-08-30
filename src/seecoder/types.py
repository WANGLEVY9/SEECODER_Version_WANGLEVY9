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
    PLAN_PROPOSED = "plan_proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    STOP_MAX_STEPS = "stop_max_steps"
    STOP_TOOL_ERROR_LIMIT = "stop_tool_error_limit"
    STOP_CONTEXT_BUDGET = "stop_context_budget"
    STOP_TASK_TIMEOUT = "stop_task_timeout"
    FAILED_MODEL = "failed_model"
    FAILED_PROTOCOL = "failed_protocol"
    CANCELLED = "cancelled"


class Mode(StrEnum):
    """How the agent treats tool execution and human oversight."""

    AUTO = "auto"  # run allowed tools without interruption
    PLAN = "plan"  # inspect only; propose mutations as a plan for review
    ASK = "ask"    # pause for per-action approval on mutating tools


class ApprovalDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


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
class Usage:
    """Token usage for one model response (or an accumulated total)."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    def plus(self, other: Usage) -> Usage:
        if other is None:
            return self
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One proposed mutation captured while the agent is in plan mode."""

    tool: str
    arguments: dict[str, Any]
    description: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """One model turn after provider-specific output has been normalized."""

    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    model: str | None = None
    reasoning_content: str | None = None
    usage: Usage | None = None


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
class StreamEvent:
    """One incremental piece of a streaming model response.

    kind: content_delta | tool_call_delta | reasoning_delta | done
    The final 'done' event carries the fully assembled ModelResponse.
    """

    kind: str
    text: str = ""
    index: int = -1
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    response: "ModelResponse | None" = None


@dataclass(frozen=True, slots=True)
class RunOutcome:
    state: RunState
    final_text: str
    steps: int
    trace_path: str | None = None
    plan: tuple[PlanStep, ...] = ()
    usage: Usage | None = None
    mode: Mode = Mode.AUTO
    # A persisted ASK-mode continuation.  The assistant tool-call message is
    # already present in history; these calls have not been dispatched yet.
    pending_calls: tuple[ToolCall, ...] = ()
    # A broken stream can have rendered useful text before its transport died.
    # Keep it separate from final_text so clients can present a recoverable UI.
    partial_text: str | None = None
    recoverable: bool = False
