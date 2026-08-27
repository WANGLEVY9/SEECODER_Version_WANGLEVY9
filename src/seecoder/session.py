"""Interactive, multi-turn conversation state for the coding agent."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from seecoder.approval import Policy
from seecoder.config import Settings
from seecoder.model_client import ModelClient
from seecoder.runner import AgentRunner, DEFAULT_SYSTEM_PROMPT
from seecoder.trace import NullTraceWriter, TraceWriter
from seecoder.types import ChatMessage, Mode, RunOutcome, ToolCall, Usage


Approver = Callable[[ToolCall], bool]
EventSink = Callable[[str, dict], None]


def _message_to_dict(message: ChatMessage) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls],
        "tool_call_id": message.tool_call_id,
        "reasoning_content": message.reasoning_content,
    }


def _message_from_dict(data: dict[str, object]) -> ChatMessage:
    calls = tuple(
        ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
        for c in (data.get("tool_calls") or [])
    )
    return ChatMessage(
        role=str(data.get("role", "assistant")),
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

        self.runner = AgentRunner.for_workspace(
            settings=settings,
            model_client=model_client,
            workspace=workspace,
            trace=trace,
            event_sink=event_sink,
            mode=mode,
            approver=approver,
            stream_sink=stream_sink,
            compactor=compactor,
        )
        self._messages: list[ChatMessage] = []
        self._total_usage = Usage(0, 0, 0)
        self._total_steps = 0

    @property
    def messages(self) -> list[ChatMessage]:
        return self._messages

    @property
    def total_usage(self) -> Usage:
        return self._total_usage

    @property
    def total_steps(self) -> int:
        return self._total_steps

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
        if not text.strip():
            raise ValueError("Message must be a non-empty string")
        self._messages.append(ChatMessage(role="user", content=text))
        return self._advance()

    def approve_plan(self) -> RunOutcome:
        """After a PLAN_PROPOSED outcome, switch to AUTO and execute the approved plan."""

        self.current_mode = Mode.AUTO
        self.runner.mode = Mode.AUTO
        self.runner.policy = Policy(Mode.AUTO)
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
            "version": 1,
            "mode": self.current_mode.value,
            "workspace": str(self.workspace),
            "usage": {
                "prompt_tokens": self._total_usage.prompt_tokens,
                "completion_tokens": self._total_usage.completion_tokens,
                "total_tokens": self._total_usage.total_tokens,
            },
            "steps": self._total_steps,
            "messages": [_message_to_dict(message) for message in self._messages],
        }

    def save(self, path: Path) -> Path:
        """Persist the conversation to a JSON file and return its path."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def restore(self, data: dict[str, object]) -> None:
        """Rehydrate a serialized conversation onto an already-built runner."""

        self._messages = [_message_from_dict(item) for item in (data.get("messages") or [])]
        usage = data.get("usage") or {}
        self._total_usage = Usage(
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            int(usage.get("total_tokens", 0)),
        )
        self._total_steps = int(data.get("steps", 0))
        self.current_mode = Mode(str(data.get("mode", "auto")))
        self.runner.mode = self.current_mode
        self.runner.policy = Policy(self.current_mode)

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
    ) -> Conversation:
        """Load a saved conversation and rehydrate it onto a fresh runner."""

        data = json.loads(Path(path).read_text(encoding="utf-8"))
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
        )
        conversation.restore(data)
        return conversation

    def _advance(self) -> RunOutcome:
        outcome = self.runner.run_messages(self._messages)
        if outcome.usage is not None:
            self._total_usage = self._total_usage.plus(outcome.usage)
        self._total_steps += outcome.steps
        return outcome

    def reset(self) -> None:
        self._messages = []
        self._total_usage = Usage(0, 0, 0)
        self._total_steps = 0
        self.current_mode = self.runner.mode
