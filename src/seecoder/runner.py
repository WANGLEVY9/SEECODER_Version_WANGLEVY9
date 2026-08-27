"""The independently implemented coding-agent state machine."""

from __future__ import annotations

import json
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from seecoder.config import Settings
from seecoder.context import ContextBudgetExceeded, ContextManager
from seecoder.model_client import ModelClient, ModelClientError
from seecoder.tools import (
    ApplyPatchTool,
    GitDiffTool,
    ListFilesTool,
    ReadFileTool,
    RunCommandTool,
    SearchFilesTool,
    ToolRegistry,
    WorkspaceBoundary,
    WriteFileTool,
)
from seecoder.trace import NullTraceWriter, TraceWriter
from seecoder.types import ChatMessage, RunOutcome, RunState, ToolCall, ToolResult


DEFAULT_SYSTEM_PROMPT = """You are SEECODER, an autonomous but bounded coding agent.
Work only through the supplied local tools. First inspect relevant files, make focused changes,
and use run_command to validate your work when feasible. Treat non-zero command exit codes as
evidence to investigate, not as success. Never claim a task is complete unless you have evidence.
When finished, provide a concise summary of changes, validation, and any remaining uncertainty."""

EventSink = Callable[[str, dict[str, Any]], None]


class AgentRunner:
    """Drive native tool calls until the model finishes or a named stop condition occurs."""

    def __init__(
        self,
        *,
        settings: Settings,
        model_client: ModelClient,
        tools: ToolRegistry,
        trace: TraceWriter | NullTraceWriter | None = None,
        event_sink: EventSink | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self.settings = settings
        self.model_client = model_client
        self.tools = tools
        self.context = ContextManager(settings.context_char_budget)
        self.trace = trace or NullTraceWriter()
        self.event_sink = event_sink
        self.system_prompt = system_prompt

    @classmethod
    def for_workspace(
        cls,
        *,
        settings: Settings,
        model_client: ModelClient,
        workspace: Path,
        trace: TraceWriter | NullTraceWriter | None = None,
        event_sink: EventSink | None = None,
    ) -> AgentRunner:
        boundary = WorkspaceBoundary(workspace)
        tools = ToolRegistry.create(
            [
                ListFilesTool(boundary),
                ReadFileTool(boundary),
                SearchFilesTool(boundary),
                WriteFileTool(boundary),
                ApplyPatchTool(boundary),
                GitDiffTool(boundary),
                RunCommandTool(
                    boundary,
                    default_timeout_s=settings.command_timeout_s,
                    max_timeout_s=settings.command_max_timeout_s,
                    allow_dangerous_commands=settings.allow_dangerous_commands,
                    execution_mode=settings.execution_mode,
                ),
            ]
        )
        return cls(
            settings=settings,
            model_client=model_client,
            tools=tools,
            trace=trace,
            event_sink=event_sink,
        )

    def run(self, task: str) -> RunOutcome:
        if not task.strip():
            raise ValueError("Task must be a non-empty string")
        messages = [ChatMessage(role="system", content=self.system_prompt), ChatMessage(role="user", content=task)]
        self._record("run_started", {"task": task, "max_steps": self.settings.max_steps})
        consecutive_tool_errors = 0
        try:
            for step in range(1, self.settings.max_steps + 1):
                self._record("model_request", {"step": step, "message_count": len(messages)})
                self._emit("model_request", {"step": step})
                try:
                    prepared_messages = self.context.prepare(
                        messages, preserve_complete_history=self.settings.thinking_mode == "enabled"
                    )
                except ContextBudgetExceeded as error:
                    return self._finish(RunState.STOP_CONTEXT_BUDGET, str(error), step - 1)
                try:
                    response = self.model_client.complete(prepared_messages, self.tools.schemas())
                except ModelClientError as error:
                    final_text = f"Model request failed after bounded retries: {error}"
                    return self._finish(RunState.FAILED_MODEL, final_text, step - 1)

                if self.settings.thinking_mode == "enabled" and response.tool_calls and not response.reasoning_content:
                    return self._finish(
                        RunState.FAILED_PROTOCOL,
                        "Thinking-mode tool call omitted required reasoning_content; refusing an invalid continuation.",
                        step,
                    )

                assistant_message = ChatMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                    reasoning_content=response.reasoning_content,
                )
                messages.append(assistant_message)
                self._record(
                    "model_response",
                    {
                        "step": step,
                        "model": response.model,
                        "content": response.content,
                        "tool_calls": [self._call_data(call) for call in response.tool_calls],
                        "reasoning": self._reasoning_metadata(response.reasoning_content),
                    },
                )
                if not response.tool_calls:
                    final_text = response.content or "Model ended without a final textual response."
                    return self._finish(RunState.FINAL, final_text, step)

                self._emit("tool_dispatch", {"step": step, "count": len(response.tool_calls)})
                for call in response.tool_calls:
                    result = self.tools.dispatch(call)
                    self._record(
                        "tool_result",
                        {"step": step, "call": self._call_data(call), "result": result.as_dict()},
                    )
                    self._emit(
                        "tool_result",
                        {"name": call.name, "ok": result.ok, "error": result.error.kind if result.error else None},
                    )
                    messages.append(
                        ChatMessage(
                            role="tool",
                            tool_call_id=call.id,
                            content=json.dumps(result.as_dict(), ensure_ascii=False),
                        )
                    )
                    consecutive_tool_errors = consecutive_tool_errors + 1 if not result.ok else 0
                    if consecutive_tool_errors >= self.settings.max_consecutive_tool_errors:
                        return self._finish(
                            RunState.STOP_TOOL_ERROR_LIMIT,
                            "Stopped after repeated local tool errors; inspect the trace for details.",
                            step,
                        )
            return self._finish(
                RunState.STOP_MAX_STEPS,
                f"Stopped after reaching the configured maximum of {self.settings.max_steps} model steps.",
                self.settings.max_steps,
            )
        except KeyboardInterrupt:
            return self._finish(RunState.CANCELLED, "Run cancelled by user (Ctrl+C).", 0)

    def _finish(self, state: RunState, final_text: str, steps: int) -> RunOutcome:
        self._record("run_finished", {"state": state, "steps": steps, "final_text": final_text})
        self._emit("run_finished", {"state": state, "steps": steps})
        return RunOutcome(
            state=state,
            final_text=final_text,
            steps=steps,
            trace_path=str(self.trace.path) if self.trace.path else None,
        )

    def _record(self, event: str, data: dict[str, Any]) -> None:
        self.trace.record(event, data)

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self.event_sink:
            self.event_sink(event, data)

    @staticmethod
    def _call_data(call: ToolCall) -> dict[str, str]:
        return {"id": call.id, "name": call.name, "arguments": call.arguments}

    @staticmethod
    def _reasoning_metadata(reasoning_content: str | None) -> dict[str, str | int] | None:
        if reasoning_content is None:
            return None
        return {
            "characters": len(reasoning_content),
            "sha256": hashlib.sha256(reasoning_content.encode("utf-8")).hexdigest(),
        }
