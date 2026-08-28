"""The independently implemented coding-agent state machine."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from seecoder.approval import Policy, is_read_only
from seecoder.config import Settings
from seecoder.compaction import DEFAULT_KEEP_TURNS
from seecoder.context import ContextBudgetExceeded, ContextManager, _turns, estimate_message_chars
from dataclasses import replace

from seecoder.memory import load_memory_block
from seecoder.skills import build_skill_block
from seecoder.model_client import ModelClient, ModelClientError
from seecoder.tools import (
    ApplyPatchTool,
    GitDiffTool,
    GitLogTool,
    GitShowTool,
    GitStatusTool,
    ListFilesTool,
    ListSkillsTool,
    ReadFileTool,
    RenameDirectoryTool,
    RunCommandTool,
    SearchCodeTool,
    SearchFilesTool,
    SpawnAgentTool,
    ToolRegistry,
    WebSearchTool,
    WorkspaceBoundary,
    WriteFileTool,
    FindFilesTool,
    ProjectOverviewTool,
)
from seecoder.trace import NullTraceWriter, TraceWriter
from seecoder.types import (
    ApprovalDecision,
    ChatMessage,
    Mode,
    ModelResponse,
    PlanStep,
    RunOutcome,
    RunState,
    StreamEvent,
    ToolCall,
    ToolResult,
    Usage,
)


DEFAULT_SYSTEM_PROMPT = """You are SEECODER, an autonomous but bounded coding agent.
Work only through the supplied local tools. First inspect relevant files, make focused changes,
and use run_command to validate your work when feasible. Treat non-zero command exit codes as
evidence to investigate, not as success. Never claim a task is complete unless you have evidence.
When finished, provide a concise summary of changes, validation, and any remaining uncertainty.

The desktop application owns selecting, creating, and renaming the workspace root directory.
Do not attempt to rename that root with pwd, ls, mv, or any shell command: use list_files to
inspect its contents and tell the user to use the desktop workspace menu for the root rename.
For a non-root source directory inside the workspace, use rename_directory rather than a shell command.
You may edit files inside the selected workspace only through the supplied local tools."""

Approver = Callable[[ToolCall], bool]
EventSink = Callable[[str, dict[str, Any]], None]


def _auto_allow(call: ToolCall) -> bool:
    return True


def _subagent_factory(settings: Settings, model_client: ModelClient, workspace: Path):
    """Build a factory that runs a bounded sub-agent without further sub-agent spawning."""

    def factory(name: str, task: str, max_steps: int) -> str:
        nested_settings = replace(settings, max_steps=max_steps)
        nested = AgentRunner.for_workspace(
            settings=nested_settings,
            model_client=model_client,
            workspace=workspace,
            trace=None,
            event_sink=None,
            mode=Mode.AUTO,
            approver=None,
            enable_subagents=False,
        )
        outcome = nested.run(task)
        text = (outcome.final_text or "").strip()
        return text or f"[{name}] sub-agent produced no final output."

    return factory


def _plan_description(call: ToolCall) -> str:
    try:
        arguments = json.loads(call.arguments)
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    if call.name == "write_file":
        return f"write_file {arguments.get('path', '?')} ({len(str(arguments.get('content', '')))} chars)"
    if call.name == "apply_patch":
        return f"apply_patch {arguments.get('path', '?')}"
    if call.name == "run_command":
        argv = arguments.get("argv") or arguments.get("command", "?")
        return f"run_command {argv}"
    return f"{call.name} {json.dumps(arguments, ensure_ascii=False)[:200]}"


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
        mode: Mode = Mode.AUTO,
        approver: Approver | None = None,
        memory_block: str = "",
        stream_sink: Callable[[StreamEvent], None] | None = None,
        compactor: Callable[[list[ChatMessage]], str] | None = None,
    ) -> None:
        self.settings = settings
        self.model_client = model_client
        self.tools = tools
        self.context = ContextManager(settings.context_char_budget)
        self.trace = trace or NullTraceWriter()
        self.event_sink = event_sink
        self.system_prompt = system_prompt
        self.memory_block = memory_block
        self.mode = mode
        self.policy = Policy(mode)
        self.approver = approver or _auto_allow
        self.stream_sink = stream_sink
        self.compactor = compactor

    @classmethod
    def for_workspace(
        cls,
        *,
        settings: Settings,
        model_client: ModelClient,
        workspace: Path,
        trace: TraceWriter | NullTraceWriter | None = None,
        event_sink: EventSink | None = None,
        mode: Mode = Mode.AUTO,
        approver: Approver | None = None,
        stream_sink: Callable[[StreamEvent], None] | None = None,
        compactor: Callable[[list[ChatMessage]], str] | None = None,
        enable_subagents: bool = True,
    ) -> AgentRunner:
        boundary = WorkspaceBoundary(workspace)
        tool_instances: list[Any] = [
            ListFilesTool(boundary),
            ReadFileTool(boundary),
            SearchFilesTool(boundary),
            SearchCodeTool(boundary),
            WriteFileTool(boundary),
            ApplyPatchTool(boundary),
            RenameDirectoryTool(boundary),
            GitDiffTool(boundary),
            GitStatusTool(boundary),
            GitLogTool(boundary),
            ListSkillsTool(boundary),
            GitShowTool(boundary),
            FindFilesTool(boundary),
            ProjectOverviewTool(boundary),
            RunCommandTool(
                boundary,
                default_timeout_s=settings.command_timeout_s,
                max_timeout_s=settings.command_max_timeout_s,
                allow_dangerous_commands=settings.allow_dangerous_commands,
                execution_mode=settings.execution_mode,
            ),
            WebSearchTool(),
        ]
        if enable_subagents:
            tool_instances.append(SpawnAgentTool(_subagent_factory(settings, model_client, workspace)))
        tools = ToolRegistry.create(tool_instances)
        return cls(
            settings=settings,
            model_client=model_client,
            tools=tools,
            trace=trace,
            event_sink=event_sink,
            mode=mode,
            approver=approver,
            memory_block="\n\n".join(
                part for part in (load_memory_block(workspace), build_skill_block(workspace)) if part
            ),
            stream_sink=stream_sink,
            compactor=compactor,
        )

    def run(self, task: str) -> RunOutcome:
        """Run a single task from a fresh system+user conversation."""

        if not task.strip():
            raise ValueError("Task must be a non-empty string")
        messages = [ChatMessage(role="system", content=self.build_system(self.system_prompt)), ChatMessage(role="user", content=task)]
        return self._run(messages)

    def run_messages(self, messages: list[ChatMessage]) -> RunOutcome:
        """Continue an existing conversation (system history retained by the caller)."""

        return self._run(messages)

    def build_system(self, content: str) -> str:
        """Return a system prompt with any project memory block appended."""

        if self.memory_block:
            return content + "\n\n" + self.memory_block
        return content

    def _complete_streaming(self, messages: list[ChatMessage]) -> ModelResponse:
        """Consume a streaming response, forward deltas, and return the assembled response."""

        response: ModelResponse | None = None
        for event in self.model_client.complete_stream(messages, self.tools.schemas()):  # type: ignore[attr-defined]
            if self.stream_sink is not None:
                self.stream_sink(event)
            if event.kind == "done":
                response = event.response
                break
        if response is None:
            raise ModelClientError("Streaming ended without a final response.", retryable=False)
        return response

    def _maybe_compact(self, messages: list[ChatMessage]) -> bool:
        """Compress the older history into one note when the budget is exceeded.

        Disabled in thinking mode (which must preserve reasoning_content) and whenever
        no compactor is configured; falls back to the deterministic trim in context.py.
        """

        if self.compactor is None or self.settings.thinking_mode == "enabled":
            return False
        if sum(estimate_message_chars(message) for message in messages) <= self.settings.context_char_budget:
            return False
        groups = _turns(messages)
        if len(groups) <= DEFAULT_KEEP_TURNS + 2:
            return False
        pinned = [message for group in groups[:2] for message in group]
        tail = [message for group in groups[-DEFAULT_KEEP_TURNS:] for message in group]
        prefix = [message for group in groups[2:-DEFAULT_KEEP_TURNS] for message in group]
        if not prefix:
            return False
        summary = (self.compactor(prefix) or "").strip()
        if not summary:
            return False
        compacted = ChatMessage(role="system", content="<compacted_context>\n" + summary + "\n</compacted_context>")
        messages[:] = pinned + [compacted] + tail
        self._record("context_compacted", {"summary_chars": len(summary), "removed_messages": len(prefix)})
        self._emit("context_compacted", {"summary_chars": len(summary), "removed_messages": len(prefix)})
        return True

    def _run(self, messages: list[ChatMessage]) -> RunOutcome:
        self._record("run_started", {"mode": self.mode.value, "max_steps": self.settings.max_steps})
        self._emit("run_started", {"mode": self.mode.value, "max_steps": self.settings.max_steps})
        steps = 0
        consecutive_tool_errors = 0
        plan_steps: list[PlanStep] = []
        total_usage = Usage(0, 0, 0)
        try:
            for step in range(1, self.settings.max_steps + 1):
                steps = step
                self._maybe_compact(messages)
                self._record("model_request", {"step": step, "message_count": len(messages)})
                self._emit("model_request", {"step": step})
                try:
                    prepared_messages = self.context.prepare(
                        messages, preserve_complete_history=self.settings.thinking_mode == "enabled"
                    )
                except ContextBudgetExceeded as error:
                    return self._finish(RunState.STOP_CONTEXT_BUDGET, str(error), steps - 1)
                try:
                    if self.stream_sink is not None:
                        response = self._complete_streaming(prepared_messages)
                    else:
                        response = self.model_client.complete(prepared_messages, self.tools.schemas())
                except ModelClientError as error:
                    final_text = f"Model request failed after bounded retries: {error}"
                    return self._finish(RunState.FAILED_MODEL, final_text, steps - 1)

                if response.usage is not None:
                    total_usage = total_usage.plus(response.usage)
                    self._record("usage", {"prompt_tokens": response.usage.prompt_tokens,
                                           "completion_tokens": response.usage.completion_tokens,
                                           "total_tokens": response.usage.total_tokens})
                    self._emit("usage", {"total_tokens": total_usage.total_tokens,
                                         "prompt_tokens": total_usage.prompt_tokens,
                                         "completion_tokens": total_usage.completion_tokens})

                if self.settings.thinking_mode == "enabled" and response.tool_calls and not response.reasoning_content:
                    return self._finish(
                        RunState.FAILED_PROTOCOL,
                        "Thinking-mode tool call omitted required reasoning_content; refusing an invalid continuation.",
                        steps,
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
                        "usage": {"total_tokens": total_usage.total_tokens},
                    },
                )
                if not response.tool_calls:
                    if self.mode == Mode.PLAN and plan_steps:
                        return self._finish(
                            RunState.PLAN_PROPOSED,
                            response.content or "The agent inspected the workspace and proposed a plan.",
                            steps,
                            plan=plan_steps,
                            usage=total_usage,
                        )
                    final_text = response.content or "Model ended without a final textual response."
                    return self._finish(RunState.FINAL, final_text, steps, usage=total_usage)

                self._emit(
                    "tool_dispatch",
                    {
                        "step": step,
                        "count": len(response.tool_calls),
                        "calls": [
                            {
                                "name": call.name,
                                "purpose": _plan_description(call),
                            }
                            for call in response.tool_calls
                        ],
                    },
                )
                calls = response.tool_calls
                if calls and all(is_read_only(call.name) for call in calls):
                    results = self._dispatch_parallel(calls)
                else:
                    results = [self._dispatch_with_policy(call, plan_steps) for call in calls]
                for call, result in zip(calls, results):
                    self._record(
                        "tool_result",
                        {"step": step, "call": self._call_data(call), "result": result.as_dict()},
                    )
                    self._emit(
                        "tool_result",
                        {
                            "name": call.name,
                            "ok": result.ok,
                            "error": result.error.kind if result.error else None,
                            "purpose": _plan_description(call),
                        },
                    )
                    messages.append(
                        ChatMessage(
                            role="tool",
                            tool_call_id=call.id,
                            content=json.dumps(result.as_dict(), ensure_ascii=False),
                        )
                    )
                    if not result.ok and not self._is_plan_notice(result):
                        consecutive_tool_errors += 1
                        if consecutive_tool_errors >= self.settings.max_consecutive_tool_errors:
                            return self._finish(
                                RunState.STOP_TOOL_ERROR_LIMIT,
                                "Stopped after repeated local tool errors; inspect the trace for details.",
                                steps,
                                usage=total_usage,
                            )
            return self._finish(
                RunState.STOP_MAX_STEPS,
                f"Stopped after reaching the configured maximum of {self.settings.max_steps} model steps.",
                self.settings.max_steps,
                usage=total_usage,
            )
        except KeyboardInterrupt:
            return self._finish(RunState.CANCELLED, "Run cancelled by user (Ctrl+C).", steps, usage=total_usage)

    def _dispatch_parallel(self, calls: tuple[ToolCall, ...]) -> list[ToolResult]:
        """Run independent read-only tool calls concurrently, preserving call order."""

        if len(calls) == 1:
            return [self.tools.dispatch(calls[0])]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(8, len(calls))) as executor:
            futures = [executor.submit(self.tools.dispatch, call) for call in calls]
            return [future.result() for future in futures]

    def _dispatch_with_policy(
        self,
        call: ToolCall,
        plan_steps: list[PlanStep],
    ) -> ToolResult:
        if self.mode == Mode.PLAN and not is_read_only(call.name):
            plan_steps.append(PlanStep(tool=call.name, arguments=_safe_arguments(call),
                                       description=_plan_description(call)))
            self._emit("plan_proposal", {"name": call.name,
                                         "arguments": _safe_arguments(call),
                                         "description": _plan_description(call)})
            return ToolResult.failure(
                "PlanMode",
                "Plan mode: mutations are not executed. Describe your plan in text for review.",
            )
        decision = self.policy.decide(call.name)
        if decision == ApprovalDecision.NEEDS_APPROVAL:
            self._emit("approval_request", {"name": call.name, "arguments": _safe_arguments(call)})
            self._record("approval_request", {"call": self._call_data(call)})
            if self.approver(call):
                return self.tools.dispatch(call)
            return ToolResult.failure("DeniedByUser", "User denied this action in ask mode.")
        return self.tools.dispatch(call)

    @staticmethod
    def _is_plan_notice(result: ToolResult) -> bool:
        return result.error is not None and result.error.kind == "PlanMode"

    def _finish(
        self,
        state: RunState,
        final_text: str,
        steps: int,
        *,
        plan: list[PlanStep] | None = None,
        usage: Usage | None = None,
    ) -> RunOutcome:
        outcome = RunOutcome(
            state=state,
            final_text=final_text,
            steps=steps,
            trace_path=str(self.trace.path) if self.trace.path else None,
            plan=tuple(plan or ()),
            usage=usage or Usage(0, 0, 0),
            mode=self.mode,
        )
        self._record("run_finished", {"state": state, "steps": steps, "final_text": final_text,
                                      "usage_total_tokens": outcome.usage.total_tokens})
        self._emit("run_finished", {"state": state, "steps": steps})
        return outcome

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


def _safe_arguments(call: ToolCall) -> dict[str, Any]:
    try:
        parsed = json.loads(call.arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
