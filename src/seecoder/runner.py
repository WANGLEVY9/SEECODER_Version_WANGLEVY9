"""The independently implemented coding-agent state machine."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from seecoder.approval import Policy
from seecoder.changesets import ChangeSetJournal
from seecoder.config import Settings
from seecoder.compaction import DEFAULT_KEEP_TURNS
from seecoder.context import ContextBudgetExceeded, ContextManager, _turns, estimate_message_chars
from dataclasses import replace

from seecoder.memory import load_memory_block
from seecoder.skills import build_skill_block
from seecoder.model_client import ModelClient, ModelClientError
from seecoder.tools import (
    ApplyPatchTool,
    CopyFileTool,
    CreateDirectoryTool,
    DeleteFileTool,
    GitDiffTool,
    GitLogTool,
    GitShowTool,
    GitStatusTool,
    ListFilesTool,
    ListSkillsTool,
    MoveFileTool,
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
Always answer in the same natural language as the latest user request: use Simplified Chinese for
Chinese requests and English for English requests. For mixed-language requests, use the dominant
language while preserving code, commands, paths, identifiers, and quoted text verbatim.
For cleanup, use the dedicated delete_file tool for one temporary file; never try to
remove files with rm in restricted mode. Do not delete directories or project metadata.

The selected workspace root can be renamed through the local rename_directory tool: call it with
path='.' and new_name set to one safe directory-name component. Never use pwd, ls, mv, or another
shell command to rename a directory. The tool updates the active workspace boundary and reports
the new absolute path. For a non-root source directory, pass its workspace-relative path to the
same tool. Refuse to rename protected system folders, symbolic links, or an existing destination.
You may edit files inside the selected workspace only through the supplied local tools."""

Approver = Callable[[ToolCall], bool | None]
EventSink = Callable[[str, dict[str, Any]], None]


def _auto_allow(call: ToolCall) -> bool:
    return True


class CancellationToken:
    """Thread-safe cooperative cancellation for one agent run.

    Model providers and subprocesses still have their own bounded timeouts;
    this token is checked at every safe state-machine boundary and makes the
    eventual terminal state explicit rather than relying on process killing.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


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
        cancellation_token: CancellationToken | None = None,
        changeset_journal: ChangeSetJournal | None = None,
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
        self.policy = Policy(mode, read_only_resolver=tools.is_read_only)
        self.approver = approver
        self.stream_sink = stream_sink
        self.compactor = compactor
        self.workspace_boundary: WorkspaceBoundary | None = None
        self.cancellation_token = cancellation_token or CancellationToken()
        self.changeset_journal = changeset_journal
        self._active_plan_id: str | None = None

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
        cancellation_token: CancellationToken | None = None,
        changeset_storage_dir: Path | None = None,
    ) -> AgentRunner:
        boundary = WorkspaceBoundary(workspace)
        tool_instances: list[Any] = [
            ListFilesTool(boundary),
            ReadFileTool(boundary),
            SearchFilesTool(boundary),
            SearchCodeTool(boundary),
            WriteFileTool(boundary),
            ApplyPatchTool(boundary),
            DeleteFileTool(boundary),
            CreateDirectoryTool(boundary),
            CopyFileTool(boundary),
            MoveFileTool(boundary),
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
        if changeset_storage_dir is None and getattr(trace, "path", None) is not None:
            changeset_storage_dir = trace.path.parent / "changesets"
        runner = cls(
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
            cancellation_token=cancellation_token,
            changeset_journal=ChangeSetJournal(boundary.root, changeset_storage_dir),
        )
        runner.workspace_boundary = boundary
        return runner

    def cancel(self) -> None:
        """Request cooperative cancellation of the current or next run."""

        self.cancellation_token.cancel()

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
        for event in self.model_client.complete_stream(messages, self.tools.schemas()):
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
        deadline = time.monotonic() + self.settings.task_timeout_s
        self._active_plan_id = str(uuid.uuid4()) if self.mode == Mode.PLAN else None
        if self.changeset_journal is not None:
            try:
                checkpoint = self.changeset_journal.start()
                checkpoint_data = {"changeset_id": checkpoint.id, "workspace": checkpoint.workspace}
                self._record("checkpoint_created", checkpoint_data)
                self._emit("checkpoint_created", checkpoint_data)
            except (OSError, ValueError) as error:
                warning = {"tool": "checkpoint", "message": f"{type(error).__name__}: {error}"}
                self._record("changeset_error", warning)
                self._emit("changeset_error", warning)
        self._record("run_started", {"mode": self.mode.value, "max_steps": self.settings.max_steps,
                                     "task_timeout_s": self.settings.task_timeout_s})
        self._emit("run_started", {"mode": self.mode.value, "max_steps": self.settings.max_steps,
                                   "task_timeout_s": self.settings.task_timeout_s})
        steps = 0
        consecutive_tool_errors = 0
        last_tool_error = ""
        repeated_failures: dict[str, int] = {}
        plan_steps: list[PlanStep] = []
        total_usage = Usage(0, 0, 0)
        try:
            for step in range(1, self.settings.max_steps + 1):
                steps = step
                stopped = self._interrupt_outcome(deadline, steps - 1, total_usage)
                if stopped is not None:
                    return stopped
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
                    return self._finish(
                        RunState.FAILED_MODEL, final_text, steps - 1, usage=total_usage,
                        partial_text=error.partial_text, recoverable=True,
                    )

                stopped = self._interrupt_outcome(deadline, steps, total_usage)
                if stopped is not None:
                    return stopped

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
                # ASK mode pauses before dispatching any mutation.  We keep
                # every call in this provider turn pending so a later resume
                # preserves assistant/tool-call ordering exactly.
                if (self.mode == Mode.ASK and self.approver is None
                        and any(not self.tools.is_read_only(call.name) for call in calls)):
                    first_mutation = next(call for call in calls if not self.tools.is_read_only(call.name))
                    self._emit("approval_request", {"name": first_mutation.name,
                                                   "arguments": _safe_arguments(first_mutation)})
                    self._record("approval_request", {"call": self._call_data(first_mutation),
                                                       "pending_count": len(calls)})
                    return self._finish(
                        RunState.AWAITING_APPROVAL,
                        f"Waiting for approval before running {first_mutation.name}.",
                        steps,
                        usage=total_usage,
                        pending_calls=calls,
                        recoverable=True,
                    )
                if calls and all(self.tools.is_read_only(call.name) for call in calls):
                    results = self._dispatch_parallel(calls)
                else:
                    results = [self._dispatch_with_policy(call, plan_steps) for call in calls]
                for call, result in zip(calls, results):
                    self._append_tool_result(messages, step, call, result)
                    if not result.ok and not self._is_plan_notice(result):
                        consecutive_tool_errors += 1
                        last_tool_error = f"{call.name}: {result.error.message if result.error else 'unknown error'}"
                        signature = call.name + "\0" + call.arguments
                        repeated_failures[signature] = repeated_failures.get(signature, 0) + 1
                        same_call_repeated = repeated_failures[signature] >= 2
                        if same_call_repeated or consecutive_tool_errors >= self.settings.max_consecutive_tool_errors:
                            reason = "the same failing call was repeated" if same_call_repeated else "repeated local tool errors"
                            return self._finish(
                                RunState.STOP_TOOL_ERROR_LIMIT,
                                "Stopped after " + reason + "; latest failure was " + last_tool_error + ".",
                                steps,
                                usage=total_usage,
                            )
                    elif result.ok:
                        # The limit is explicitly consecutive: a successful
                        # observation means the agent recovered and gets a
                        # fresh error budget for the next tool sequence.
                        consecutive_tool_errors = 0
                        last_tool_error = ""
                        repeated_failures.clear()
                    stopped = self._interrupt_outcome(deadline, steps, total_usage)
                    if stopped is not None:
                        return stopped
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

    def _dispatch_mutation(self, call: ToolCall) -> ToolResult:
        before: dict[str, Any] = {}
        journal_error: str | None = None
        if self.changeset_journal is not None:
            try:
                before = self.changeset_journal.capture_before(call)
            except (OSError, ValueError) as error:
                # A journal failure must not turn an otherwise valid local
                # mutation into an unreported runner crash.  The result below
                # remains observable and the UI receives an explicit warning.
                journal_error = f"{type(error).__name__}: {error}"
        result = self.tools.dispatch(call)
        if self.changeset_journal is not None:
            try:
                change = self.changeset_journal.record(call, result, before)
                if change is not None:
                    self._record("changeset_updated", change)
                    self._emit("changeset_updated", change)
                workspace_path = result.data.get("workspace_path") if isinstance(result.data, dict) else None
                if result.ok and isinstance(workspace_path, str) and result.data.get("workspace_renamed"):
                    self.changeset_journal.update_workspace(Path(workspace_path))
            except (OSError, ValueError) as error:
                journal_error = f"{type(error).__name__}: {error}"
        if journal_error is not None:
            warning = {"tool": call.name, "message": journal_error}
            self._record("changeset_error", warning)
            self._emit("changeset_error", warning)
        return result

    def resume_pending(
        self, messages: list[ChatMessage], pending_calls: tuple[ToolCall, ...], *, approved: bool
    ) -> RunOutcome:
        """Resolve one persisted ASK-mode decision and continue the same turn.

        One approval authorizes exactly one mutating call.  If the model emitted
        additional mutations in the same turn, execution pauses again before
        each one; this keeps the approval boundary explicit and durable.
        """

        if self.mode != Mode.ASK:
            raise ValueError("Only ASK-mode conversations can resolve a pending approval.")
        if not pending_calls:
            raise ValueError("There is no pending tool call to approve or deny.")
        for index, call in enumerate(pending_calls):
            if not self.tools.is_read_only(call.name):
                result = (
                    self._dispatch_mutation(call)
                    if approved
                    else ToolResult.failure("DeniedByUser", "User denied this action in ask mode.")
                )
                self._append_tool_result(messages, 0, call, result)
                remaining = pending_calls[index + 1 :]
                next_mutation = next((item for item in remaining if not self.tools.is_read_only(item.name)), None)
                # Dispatch any read-only observations before a later mutation.
                read_only_prefix: list[ToolCall] = []
                while remaining and self.tools.is_read_only(remaining[0].name):
                    read_only_prefix.append(remaining[0])
                    remaining = remaining[1:]
                if read_only_prefix:
                    for read_call, read_result in zip(read_only_prefix, self._dispatch_parallel(tuple(read_only_prefix))):
                        self._append_tool_result(messages, 0, read_call, read_result)
                if next_mutation is not None and remaining:
                    self._emit("approval_request", {"name": next_mutation.name,
                                                   "arguments": _safe_arguments(next_mutation)})
                    self._record("approval_request", {"call": self._call_data(next_mutation),
                                                       "pending_count": len(remaining)})
                    return self._finish(
                        RunState.AWAITING_APPROVAL,
                        f"Waiting for approval before running {next_mutation.name}.", 0,
                        pending_calls=remaining, recoverable=True,
                    )
                return self._run(messages)
            # A pending sequence normally begins with read-only calls only if
            # the provider mixed them with a mutation.  Execute those safely.
            self._append_tool_result(messages, 0, call, self.tools.dispatch(call))
        return self._run(messages)

    def _append_tool_result(
        self, messages: list[ChatMessage], step: int, call: ToolCall, result: ToolResult
    ) -> None:
        self._record("tool_result", {"step": step, "call": self._call_data(call), "result": result.as_dict()})
        self._emit(
            "tool_result",
            {"name": call.name, "ok": result.ok, "error": result.error.kind if result.error else None,
             "purpose": _plan_description(call), "data": result.data, "meta": result.meta},
        )
        messages.append(ChatMessage(role="tool", tool_call_id=call.id,
                                    content=json.dumps(result.as_dict(), ensure_ascii=False)))

    def _interrupt_outcome(self, deadline: float, steps: int, usage: Usage) -> RunOutcome | None:
        if self.cancellation_token.cancelled:
            return self._finish(RunState.CANCELLED, "Run cancelled by user.", steps, usage=usage,
                                recoverable=True)
        if time.monotonic() >= deadline:
            return self._finish(
                RunState.STOP_TASK_TIMEOUT,
                f"Stopped after exceeding the task timeout of {self.settings.task_timeout_s:g} seconds.",
                steps, usage=usage, recoverable=True,
            )
        return None

    def _dispatch_with_policy(
        self,
        call: ToolCall,
        plan_steps: list[PlanStep],
    ) -> ToolResult:
        if self.mode == Mode.PLAN and not self.tools.is_read_only(call.name):
            plan_steps.append(PlanStep(tool=call.name, arguments=_safe_arguments(call),
                                       description=_plan_description(call)))
            self._emit("plan_proposal", {"name": call.name,
                                      "plan_id": self._active_plan_id,
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
            if self.approver is not None and self.approver(call):
                return self._dispatch_mutation(call)
            return ToolResult.failure("DeniedByUser", "User denied this action in ask mode.")
        if self.tools.is_read_only(call.name):
            return self.tools.dispatch(call)
        return self._dispatch_mutation(call)

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
        pending_calls: tuple[ToolCall, ...] = (),
        partial_text: str | None = None,
        recoverable: bool = False,
    ) -> RunOutcome:
        if self.changeset_journal is not None:
            try:
                self.changeset_journal.finish(state.value)
            except (OSError, ValueError) as error:
                warning = {"tool": "checkpoint", "message": f"{type(error).__name__}: {error}"}
                self._record("changeset_error", warning)
                self._emit("changeset_error", warning)
        outcome = RunOutcome(
            state=state,
            final_text=final_text,
            steps=steps,
            trace_path=str(self.trace.path) if self.trace.path else None,
            plan=tuple(plan or ()),
            plan_id=self._active_plan_id,
            usage=usage or Usage(0, 0, 0),
            mode=self.mode,
            pending_calls=pending_calls,
            partial_text=partial_text,
            recoverable=recoverable,
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
