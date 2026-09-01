"""Interactive, multi-turn conversation state for the coding agent."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from seecoder.approval import Policy
from seecoder.config import Settings
from seecoder.model_client import ModelClient
from seecoder.plans import PlanStatus, TaskPlan
from seecoder.runner import AgentRunner, DEFAULT_SYSTEM_PROMPT
from seecoder.trace import NullTraceWriter, TraceWriter
from seecoder.types import ChatMessage, Mode, RunOutcome, RunState, ToolCall, Usage


SNAPSHOT_VERSION = 3
_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}


class SnapshotValidationError(ValueError):
    """Raised before an untrusted session snapshot reaches the model client."""


Approver = Callable[[ToolCall], bool | None]
EventSink = Callable[[str, dict], None]


def _message_to_dict(message: ChatMessage) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls],
        "tool_call_id": message.tool_call_id,
        "reasoning_content": message.reasoning_content,
    }


def _tool_call_from_dict(data: object, *, label: str) -> ToolCall:
    if not isinstance(data, dict) or set(data) != {"id", "name", "arguments"}:
        raise SnapshotValidationError(f"{label} must contain exactly id, name, and arguments.")
    if not all(isinstance(data[key], str) and data[key] for key in ("id", "name", "arguments")):
        raise SnapshotValidationError(f"{label} fields id, name, and arguments must be non-empty strings.")
    try:
        arguments = json.loads(data["arguments"])
    except json.JSONDecodeError as error:
        raise SnapshotValidationError(f"{label}.arguments must be valid JSON.") from error
    if not isinstance(arguments, dict):
        raise SnapshotValidationError(f"{label}.arguments must encode a JSON object.")
    return ToolCall(id=data["id"], name=data["name"], arguments=data["arguments"])


def _message_from_dict(data: object, *, index: int) -> ChatMessage:
    if not isinstance(data, dict):
        raise SnapshotValidationError(f"messages[{index}] must be an object.")
    required = {"role", "content", "tool_calls", "tool_call_id", "reasoning_content"}
    if set(data) != required:
        raise SnapshotValidationError(f"messages[{index}] has unsupported or missing fields.")
    role = data["role"]
    if not isinstance(role, str) or role not in _MESSAGE_ROLES:
        raise SnapshotValidationError(f"messages[{index}].role is invalid.")
    if data["content"] is not None and not isinstance(data["content"], str):
        raise SnapshotValidationError(f"messages[{index}].content must be a string or null.")
    if data["reasoning_content"] is not None and not isinstance(data["reasoning_content"], str):
        raise SnapshotValidationError(f"messages[{index}].reasoning_content must be a string or null.")
    if data["tool_call_id"] is not None and not isinstance(data["tool_call_id"], str):
        raise SnapshotValidationError(f"messages[{index}].tool_call_id must be a string or null.")
    raw_calls = data["tool_calls"]
    if not isinstance(raw_calls, list):
        raise SnapshotValidationError(f"messages[{index}].tool_calls must be a list.")
    calls = tuple(_tool_call_from_dict(call, label=f"messages[{index}].tool_calls[{call_index}]")
                  for call_index, call in enumerate(raw_calls))
    if role == "tool" and (calls or not data["tool_call_id"]):
        raise SnapshotValidationError(f"messages[{index}] tool messages require tool_call_id and no tool_calls.")
    if role != "tool" and data["tool_call_id"] is not None:
        raise SnapshotValidationError(f"messages[{index}] only tool messages may contain tool_call_id.")
    if calls and role != "assistant":
        raise SnapshotValidationError(f"messages[{index}] only assistant messages may contain tool_calls.")
    return ChatMessage(
        role=role,
        content=data.get("content"),
        tool_calls=calls,
        tool_call_id=data.get("tool_call_id"),
        reasoning_content=data.get("reasoning_content"),
    )


class Conversation:
    """Own the growing message history so a user can send follow-up turns.

    Each send() continues the same model context: the runner appends assistant and
    tool messages to the shared list, and the context manager trims only when the
    budget is exceeded. The conversation also tracks cumulative token usage and steps.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        model_client: ModelClient,
        workspace: Path,
        trace: TraceWriter | NullTraceWriter | None = None,
        event_sink: EventSink | None = None,
        mode: Mode = Mode.AUTO,
        approver: Approver | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        stream_sink: Callable[[object], None] | None = None,
        compactor: Callable[[list[ChatMessage]], str] | None = None,
    ) -> None:
        self.settings = settings
        self.model_client = model_client
        self.workspace = workspace
        self.event_sink = event_sink
        self.system_prompt = system_prompt
        self.current_mode = mode
        self.approver = approver
        self._task_plan: TaskPlan | None = None

        self.runner = AgentRunner.for_workspace(
            settings=settings,
            model_client=model_client,
            workspace=workspace,
            trace=trace,
            event_sink=self._handle_runner_event,
            mode=mode,
            approver=approver,
            stream_sink=stream_sink,
            compactor=compactor,
        )
        self._messages: list[ChatMessage] = []
        self._total_usage = Usage(0, 0, 0)
        self._total_steps = 0
        self._pending_calls: tuple[ToolCall, ...] = ()

    @property
    def messages(self) -> list[ChatMessage]:
        return self._messages

    @property
    def total_usage(self) -> Usage:
        return self._total_usage

    @property
    def total_steps(self) -> int:
        return self._total_steps

    @property
    def pending_calls(self) -> tuple[ToolCall, ...]:
        return self._pending_calls

    @property
    def task_plan(self) -> TaskPlan | None:
        return self._task_plan

    def start(self, task: str) -> RunOutcome:
        """Begin a fresh conversation with the given task."""

        if self._messages:
            raise ValueError("Conversation already started; use send() for a follow-up turn.")
        if not task.strip():
            raise ValueError("Task must be a non-empty string")
        self._messages = [ChatMessage(role="system", content=self.runner.build_system(self.system_prompt)), ChatMessage(role="user", content=task)]
        return self._advance()

    def send(self, text: str) -> RunOutcome:
        """Continue an in-progress or completed conversation with a new user turn."""

        if not self._messages:
            return self.start(text)
        if self._pending_calls:
            raise ValueError("An approval decision is required before sending a new message.")
        if not text.strip():
            raise ValueError("Message must be a non-empty string")
        self._messages.append(ChatMessage(role="user", content=text))
        return self._advance()

    def resolve_approval(self, approved: bool) -> RunOutcome:
        """Resume one persisted ASK-mode tool decision without replaying a model turn."""

        if not self._pending_calls:
            raise ValueError("There is no pending approval to resolve.")
        pending = self._pending_calls
        self._pending_calls = ()
        outcome = self.runner.resume_pending(self._messages, pending, approved=approved)
        return self._consume_outcome(outcome)

    def approve_plan(self) -> RunOutcome:
        """After a PLAN_PROPOSED outcome, switch to AUTO and execute the approved plan."""

        self.current_mode = Mode.AUTO
        self.runner.mode = Mode.AUTO
        self.runner.policy = Policy(Mode.AUTO, read_only_resolver=self.runner.tools.is_read_only)
        if self._task_plan is not None:
            self._task_plan.transition(PlanStatus.EXECUTING)
            self._emit_plan_state()
        self._messages.append(
            ChatMessage(role="user", content="The proposed plan is approved. Execute it now.")
        )
        return self._advance()

    def summary(self) -> RunOutcome:
        """Ask the model to summarize the conversation so far (still respects the mode)."""

        return self.send("Please summarize the current state of this task and what remains to be done.")

    def to_dict(self) -> dict[str, object]:
        """Serialize the conversation so it can be resumed later."""

        return {
            "version": SNAPSHOT_VERSION,
            "mode": self.current_mode.value,
            "workspace": str(self.workspace),
            "usage": {
                "prompt_tokens": self._total_usage.prompt_tokens,
                "completion_tokens": self._total_usage.completion_tokens,
                "total_tokens": self._total_usage.total_tokens,
            },
            "steps": self._total_steps,
            "messages": [_message_to_dict(message) for message in self._messages],
            "pending_approval": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in self._pending_calls
            ],
            "task_plan": self._task_plan.to_dict() if self._task_plan is not None else None,
        }

    def save(self, path: Path) -> Path:
        """Persist the conversation to a JSON file and return its path."""

        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        # Replace the snapshot atomically so a desktop/CLI interruption cannot
        # leave a truncated JSON file that makes the next resume impossible.
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return path

    def restore(self, data: dict[str, object]) -> None:
        """Rehydrate a serialized conversation onto an already-built runner."""

        self._validate_snapshot(data)
        self._messages = [_message_from_dict(item, index=index) for index, item in enumerate(data["messages"])]
        usage = data["usage"]
        self._total_usage = Usage(
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            int(usage.get("total_tokens", 0)),
        )
        self._total_steps = int(data["steps"])
        self.current_mode = Mode(str(data["mode"]))
        self._pending_calls = tuple(
            _tool_call_from_dict(item, label=f"pending_approval[{index}]")
            for index, item in enumerate(data["pending_approval"])
        )
        self._task_plan = TaskPlan.from_dict(data["task_plan"]) if data["task_plan"] is not None else None
        if self._pending_calls and self.current_mode != Mode.ASK:
            raise SnapshotValidationError("Only ASK-mode snapshots may contain pending approvals.")
        self.runner.mode = self.current_mode
        self.runner.policy = Policy(self.current_mode, read_only_resolver=self.runner.tools.is_read_only)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        settings: Settings,
        model_client: ModelClient,
        workspace: Path | None = None,
        trace: TraceWriter | NullTraceWriter | None = None,
        event_sink: EventSink | None = None,
        approver: Approver | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        stream_sink: Callable[[object], None] | None = None,
        compactor: Callable[[list[ChatMessage]], str] | None = None,
    ) -> Conversation:
        """Load a saved conversation and rehydrate it onto a fresh runner."""

        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SnapshotValidationError(f"Could not read session snapshot: {error}") from error
        if not isinstance(data, dict):
            raise SnapshotValidationError("Session snapshot root must be an object.")
        data = cls._upgrade_snapshot(data)
        cls._validate_snapshot(data)
        selected = Path(str(data.get("workspace"))) if workspace is None else workspace
        conversation = cls(
            settings=settings,
            model_client=model_client,
            workspace=selected,
            trace=trace,
            event_sink=event_sink,
            mode=Mode(str(data.get("mode", "auto"))),
            approver=approver,
            system_prompt=system_prompt,
            stream_sink=stream_sink,
            compactor=compactor,
        )
        conversation.restore(data)
        return conversation

    def _advance(self) -> RunOutcome:
        outcome = self.runner.run_messages(self._messages)
        return self._consume_outcome(outcome)

    def _handle_runner_event(self, event: str, data: dict) -> None:
        if event == "plan_proposal":
            plan_id = str(data.get("plan_id") or "")
            if self._task_plan is None or (plan_id and self._task_plan.id != plan_id):
                task = next((message.content or "" for message in reversed(self._messages) if message.role == "user"), "")
                self._task_plan = TaskPlan.from_steps(task, (), plan_id=plan_id or None)
            from seecoder.types import PlanStep
            self._task_plan.add_step(PlanStep(
                tool=str(data.get("name") or "unknown"),
                arguments=dict(data.get("arguments") or {}),
                description=str(data.get("description") or data.get("name") or "Plan step"),
            ))
        elif event == "tool_dispatch" and self._task_plan is not None and self._task_plan.status == PlanStatus.EXECUTING:
            for call in data.get("calls") or ():
                if isinstance(call, dict):
                    self._task_plan.mark_tool_started(str(call.get("name") or ""))
            self._emit_plan_state()
        elif event == "tool_result" and self._task_plan is not None and self._task_plan.status == PlanStatus.EXECUTING:
            self._task_plan.mark_tool_result(
                str(data.get("name") or ""), bool(data.get("ok")),
                str(data.get("error") or ("completed" if data.get("ok") else "failed")),
            )
            self._emit_plan_state()
        if self.event_sink is not None:
            self.event_sink(event, data)

    def _emit_plan_state(self) -> None:
        if self.event_sink is None or self._task_plan is None:
            return
        self.event_sink("plan_state", {
            "plan_id": self._task_plan.id,
            "status": self._task_plan.status.value,
            "task": self._task_plan.task,
            "items": self._task_plan.to_dict()["items"],
        })

    def _consume_outcome(self, outcome: RunOutcome) -> RunOutcome:
        # A failed provider request does not produce an assistant message, so
        # the next user turn would otherwise be serialized as two adjacent
        # ``user`` messages. Record the failure as an assistant observation;
        # this keeps the provider-neutral history well formed and gives the
        # next request enough context to recover without replaying tools.
        if outcome.state == RunState.FAILED_MODEL and (
            not self._messages or self._messages[-1].role == "user"
        ):
            self._messages.append(
                ChatMessage(
                    role="assistant",
                    content=("[Previous model attempt failed; continue from the user's latest request.]\n"
                             + ("[Partial streamed output preserved below]\n" + outcome.partial_text + "\n"
                                if outcome.partial_text else "") + outcome.final_text),
                )
            )
        if outcome.plan and self._task_plan is None:
            self._task_plan = TaskPlan.from_steps(
                next((message.content or "" for message in reversed(self._messages) if message.role == "user"), ""),
                outcome.plan,
                plan_id=outcome.plan_id,
            )
        if self._task_plan is not None:
            if outcome.state == RunState.PLAN_PROPOSED:
                self._task_plan.transition(PlanStatus.PROPOSED)
                self._emit_plan_state()
            elif outcome.state == RunState.FINAL and self._task_plan.status == PlanStatus.EXECUTING:
                self._task_plan.transition(PlanStatus.VERIFYING)
                self._emit_plan_state()
                self._task_plan.transition(PlanStatus.COMPLETED)
                self._emit_plan_state()
            elif outcome.state not in {RunState.AWAITING_APPROVAL, RunState.PLAN_PROPOSED} and self._task_plan.status in {PlanStatus.EXECUTING, PlanStatus.VERIFYING}:
                self._task_plan.transition(PlanStatus.FAILED, evidence=outcome.final_text)
                self._emit_plan_state()
        # A local root rename updates the shared boundary. Persist that new
        # path so resume-after-restart does not point at the old directory.
        if self.runner.workspace_boundary is not None:
            self.workspace = self.runner.workspace_boundary.root
        if outcome.usage is not None:
            self._total_usage = self._total_usage.plus(outcome.usage)
        self._total_steps += outcome.steps
        self._pending_calls = outcome.pending_calls if outcome.state == RunState.AWAITING_APPROVAL else ()
        return outcome

    def reset(self) -> None:
        self._messages = []
        self._total_usage = Usage(0, 0, 0)
        self._total_steps = 0
        self._pending_calls = ()
        self._task_plan = None
        self.current_mode = self.runner.mode

    @staticmethod
    def _validate_snapshot(data: dict[str, object]) -> None:
        required = {"version", "mode", "workspace", "usage", "steps", "messages", "pending_approval", "task_plan"}
        if set(data) != required:
            raise SnapshotValidationError("Session snapshot has unsupported or missing top-level fields.")
        if data["version"] != SNAPSHOT_VERSION:
            raise SnapshotValidationError(
                f"Unsupported session snapshot version {data['version']!r}; expected {SNAPSHOT_VERSION}."
            )
        if not isinstance(data["workspace"], str) or not data["workspace"]:
            raise SnapshotValidationError("Session snapshot workspace must be a non-empty string.")
        try:
            Mode(str(data["mode"]))
        except ValueError as error:
            raise SnapshotValidationError("Session snapshot mode is invalid.") from error
        if not isinstance(data["steps"], int) or data["steps"] < 0:
            raise SnapshotValidationError("Session snapshot steps must be a non-negative integer.")
        if not isinstance(data["messages"], list) or not isinstance(data["pending_approval"], list):
            raise SnapshotValidationError("Session snapshot messages and pending_approval must be lists.")
        if data["task_plan"] is not None:
            try:
                TaskPlan.from_dict(data["task_plan"])
            except (TypeError, ValueError) as error:
                raise SnapshotValidationError("Session snapshot task_plan is invalid.") from error
        usage = data["usage"]
        if not isinstance(usage, dict) or set(usage) != {"prompt_tokens", "completion_tokens", "total_tokens"}:
            raise SnapshotValidationError("Session snapshot usage fields are invalid.")
        if any(not isinstance(value, int) or value < 0 for value in usage.values()):
            raise SnapshotValidationError("Session snapshot usage values must be non-negative integers.")
        expected_tool_ids: list[str] = []
        for index, raw_message in enumerate(data["messages"]):
            parsed = _message_from_dict(raw_message, index=index)
            if parsed.role == "assistant" and parsed.tool_calls:
                if expected_tool_ids:
                    raise SnapshotValidationError("A new assistant tool-call turn appeared before prior tool results.")
                expected_tool_ids = [call.id for call in parsed.tool_calls]
            elif parsed.role == "tool":
                if not expected_tool_ids or parsed.tool_call_id != expected_tool_ids.pop(0):
                    raise SnapshotValidationError("Tool result order does not match the preceding assistant tool calls.")
            elif expected_tool_ids:
                raise SnapshotValidationError("A tool-call turn is missing one or more tool results.")
        pending_ids = [
            _tool_call_from_dict(raw, label=f"pending_approval[{index}]").id
            for index, raw in enumerate(data["pending_approval"])
        ]
        if expected_tool_ids != pending_ids:
            raise SnapshotValidationError("Pending approval calls must exactly match the unfinished assistant tool-call turn.")

    @staticmethod
    def _upgrade_snapshot(data: dict[str, object]) -> dict[str, object]:
        """Migrate v1/v2 durable formats before strict v3 validation."""

        if data.get("version") not in {1, 2}:
            return data
        legacy_fields = {"version", "mode", "workspace", "usage", "steps", "messages"}
        if data.get("version") == 2:
            legacy_fields.add("pending_approval")
        if set(data) != legacy_fields:
            raise SnapshotValidationError("Legacy session snapshot has unsupported or missing fields.")
        upgraded = dict(data)
        upgraded["version"] = SNAPSHOT_VERSION
        if data.get("version") == 1:
            upgraded["pending_approval"] = []
        upgraded["task_plan"] = None
        return upgraded
