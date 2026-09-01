"""Command-line entry point for a transparent single-task or interactive agent run."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from seecoder import __version__
from seecoder.changesets import ChangeSetJournal
from seecoder.config import ConfigError, Settings
from seecoder.model_client import OpenAICompatibleClient, RetryingModelClient
from seecoder.runner import AgentRunner
from seecoder.session import Conversation
from seecoder.trace import TraceWriter
from seecoder.types import Mode, RunState, ToolCall


EVENT_PROTOCOL_VERSION = 3


class EventJsonEmitter:
    """Versioned, ordered event envelope shared by CLI and desktop clients."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self.run_id = ""
        self.sequence = 0

    def begin_run(self) -> str:
        self.run_id = str(uuid.uuid4())
        return self.run_id

    def emit(self, event: str, data: dict[str, Any]) -> None:
        self.sequence += 1
        print(json.dumps({"protocol_version": EVENT_PROTOCOL_VERSION, "session_id": self.session_id,
                          "run_id": self.run_id, "sequence": self.sequence,
                          "event": event, "data": data}, ensure_ascii=False, default=str), flush=True)

    def stream(self, event: Any) -> None:
        if event.kind == "content_delta":
            self.emit("token", {"text": event.text})
        elif event.kind == "reasoning_delta":
            self.emit("reasoning", {"text": event.text})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seecoder", description="A small, auditable coding agent with local tools."
    )
    parser.add_argument("--version", action="version", version=f"seecoder {__version__}")
    subcommands = parser.add_subparsers(dest="subcommand", required=True)
    run = subcommands.add_parser("run", help="run one coding task")
    run.add_argument("task", help="natural-language coding task")
    run.add_argument("--workspace", type=Path, default=Path.cwd(), help="existing directory the agent may edit")
    run.add_argument("--env-file", type=Path, help="optional untracked dotenv-style configuration file")
    run.add_argument("--trace-dir", type=Path, help="directory for ignored JSONL execution traces")
    run.add_argument("--max-steps", type=int, help="override SEECODER_MAX_STEPS for this run")
    run.add_argument(
        "--mode", choices=["auto", "plan", "ask"], default=None,
        help="auto: run tools freely; plan: inspect only and propose a plan; ask: confirm mutations",
    )
    run.add_argument("--session-id", help="stable local session identifier for the event protocol")
    run.add_argument(
        "--auto-approve", action="store_true",
        help="treat ask mode like auto for approval (never prompt)",
    )
    run.add_argument(
        "--allow-dangerous-commands",
        action="store_true",
        help="permit commands blocked by the conservative destructive-command guard",
    )
    run.add_argument(
        "--host-shell",
        action="store_true",
        help="opt into legacy host-shell execution; it is not an operating-system sandbox",
    )
    run.add_argument("--quiet", action="store_true", help="hide intermediate events")
    run.add_argument(
        "--event-json",
        action="store_true",
        help="emit local runner events as JSONL for the bundled desktop UI",
    )

    chat = subcommands.add_parser("chat", help="start an interactive multi-turn conversation")
    chat.add_argument("--workspace", type=Path, default=Path.cwd(), help="existing directory the agent may edit")
    chat.add_argument("--env-file", type=Path, help="optional untracked dotenv-style configuration file")
    chat.add_argument("--trace-dir", type=Path, help="directory for ignored JSONL execution traces")
    chat.add_argument("--max-steps", type=int, help="override SEECODER_MAX_STEPS for each turn")
    chat.add_argument("--mode", choices=["auto", "plan", "ask"], default=None,
                      help="auto: run tools freely; plan: inspect only and propose a plan; ask: confirm mutations")
    chat.add_argument("--auto-approve", action="store_true", help="never prompt for approval in ask mode")
    chat.add_argument("--allow-dangerous-commands", action="store_true",
                      help="permit commands blocked by the conservative destructive-command guard")
    chat.add_argument("--host-shell", action="store_true",
                      help="opt into legacy host-shell execution; it is not an operating-system sandbox")
    chat.add_argument("--quiet", action="store_true", help="hide intermediate events")
    chat.add_argument("--event-json", action="store_true",
                      help="emit local runner events as JSONL for the bundled desktop UI")
    chat.add_argument("--resume", type=Path, help="resume a saved conversation from this JSON file")
    chat.add_argument("--save", type=Path, help="save the conversation to this JSON file after each turn")
    chat.add_argument("--session-id", help="stable local session identifier for the event protocol")

    rollback = subcommands.add_parser("rollback-changeset", help="safely roll back one local ChangeSet")
    rollback.add_argument("--workspace", type=Path, required=True, help="workspace owning the ChangeSet")
    rollback.add_argument("--journal-dir", type=Path, required=True, help="external ChangeSet journal directory")
    rollback.add_argument("--changeset-id", required=True, help="UUID of the ChangeSet to roll back")
    return parser


def _event_printer(event: str, data: dict[str, Any]) -> None:
    if event == "model_request":
        print(f"[step {data['step']}] requesting model")
    elif event == "tool_dispatch":
        print(f"[step {data['step']}] dispatching {data['count']} tool call(s)")
    elif event == "tool_result":
        suffix = "ok" if data["ok"] else f"error={data['error']}"
        print(f"  - {data['name']}: {suffix}")
    elif event == "approval_request":
        print(f"  ? approval required for {data['name']}")
    elif event == "plan_proposal":
        print(f"  ~ plan step: {data.get('description', data['name'])}")


def _event_json_printer(event: str, data: dict[str, Any]) -> None:
    """Emit a deliberately small local protocol for the zero-dependency desktop UI."""

    print(json.dumps({"event": event, "data": data}, ensure_ascii=False), flush=True)


def _token_json_printer(event: Any) -> None:
    """Forward streaming content deltas as token events for the desktop UI."""

    if event.kind == "content_delta":
        print(json.dumps({"event": "token", "data": {"text": event.text}}, ensure_ascii=False), flush=True)
    elif event.kind == "reasoning_delta":
        # Reasoning is surfaced as a separate, clearly labelled local event so
        # the UI can distinguish model thinking from the final answer stream.
        print(json.dumps({"event": "reasoning", "data": {"text": event.text}}, ensure_ascii=False), flush=True)


def _auto_approver(call: ToolCall) -> bool:
    return True


def _stdin_approver(stream: Any) -> Any:
    def decide(call: ToolCall) -> bool:
        try:
            line = stream.readline()
        except Exception:
            return False
        return bool(line) and line.strip().lower().startswith("y")

    return decide


def _make_compactor(settings: Settings, client: Any) -> Any:
    """Return a model-driven history compactor, or None when compaction is disabled."""

    if not settings.compaction_enabled:
        return None
    from seecoder.compaction import summarize

    def compactor(prefix: Any) -> str:
        return summarize(client, prefix)

    return compactor


def _make_approver(mode: Mode, auto_approve: bool, stream: Any) -> Any:
    if mode != Mode.ASK:
        return _auto_approver
    if auto_approve:
        return _auto_approver
    return _stdin_approver(stream)


def _outcome_data(outcome: Any) -> dict[str, Any]:
    return {
        "state": outcome.state, "final_text": outcome.final_text, "steps": outcome.steps,
        "trace_path": outcome.trace_path, "mode": outcome.mode.value,
        "plan_id": outcome.plan_id,
        "plan": [asdict(step) for step in outcome.plan],
        "usage": {"total_tokens": outcome.usage.total_tokens} if outcome.usage else {},
        "pending_calls": [{"id": call.id, "name": call.name, "arguments": call.arguments}
                          for call in outcome.pending_calls],
        "partial_text": outcome.partial_text,
        "recoverable": outcome.recoverable or outcome.state in {
            RunState.FAILED_MODEL, RunState.FAILED_PROTOCOL, RunState.STOP_CONTEXT_BUDGET,
            RunState.STOP_TASK_TIMEOUT, RunState.CANCELLED, RunState.AWAITING_APPROVAL,
        },
    }


def _exit_code(state: RunState) -> int:
    return {
        RunState.FINAL: 0,
        RunState.PLAN_PROPOSED: 0,
        RunState.AWAITING_APPROVAL: 0,
        RunState.STOP_MAX_STEPS: 3,
        RunState.STOP_TOOL_ERROR_LIMIT: 3,
        RunState.STOP_CONTEXT_BUDGET: 3,
        RunState.STOP_TASK_TIMEOUT: 3,
        RunState.FAILED_MODEL: 4,
        RunState.FAILED_PROTOCOL: 4,
        RunState.CANCELLED: 130,
    }.get(state, 1)


def _is_inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _print_outcome(outcome: Any, quiet: bool = False) -> None:
    if quiet:
        return
    print(f"\n[{outcome.state}] {outcome.final_text}")
    for step in outcome.plan:
        print(f"  plan: {step.description}")
    if outcome.usage is not None:
        print(f"  usage: {outcome.usage.total_tokens} tokens (prompt {outcome.usage.prompt_tokens} + completion {outcome.usage.completion_tokens})")
    if outcome.trace_path:
        print(f"Trace: {outcome.trace_path}")


def _build_common(args: Any, settings: Settings, trace: TraceWriter) -> tuple[RetryingModelClient, object]:
    client = RetryingModelClient(OpenAICompatibleClient(settings), retries=settings.model_retries)
    return client, trace


def _run_run(args: Any, settings: Settings, trace: TraceWriter, workspace: Path) -> int:
    mode = Mode(args.mode) if args.mode else settings.mode
    approver = _make_approver(mode, args.auto_approve, sys.stdin)
    client = RetryingModelClient(OpenAICompatibleClient(settings), retries=settings.model_retries)
    emitter = EventJsonEmitter(getattr(args, "session_id", None)) if args.event_json else None
    if emitter is not None:
        emitter.begin_run()
    runner = AgentRunner.for_workspace(
        settings=settings,
        model_client=client,
        workspace=workspace,
        trace=trace,
        event_sink=emitter.emit if emitter is not None else (None if args.quiet else _event_printer),
        mode=mode,
        approver=approver,
        stream_sink=emitter.stream if emitter is not None else None,
        compactor=_make_compactor(settings, client),
    )
    outcome = runner.run(args.task)
    if emitter is not None:
        emitter.emit("run_outcome", _outcome_data(outcome))
    else:
        _print_outcome(outcome, quiet=args.quiet)
    return _exit_code(outcome.state)


def _run_chat(args: Any, settings: Settings, trace: TraceWriter, workspace: Path) -> int:
    mode = Mode(args.mode) if args.mode else settings.mode
    # Event-json clients receive a durable approval event and send y/n back to
    # this process.  They must never block inside a tool dispatcher.
    approver = None if args.event_json and mode == Mode.ASK and not args.auto_approve else _make_approver(mode, args.auto_approve, sys.stdin)
    client = RetryingModelClient(OpenAICompatibleClient(settings), retries=settings.model_retries)
    emitter = EventJsonEmitter(getattr(args, "session_id", None)) if args.event_json else None
    event_sink = emitter.emit if emitter is not None else (None if args.quiet else _event_printer)
    resume = getattr(args, "resume", None)
    if resume is not None:
        # The snapshot owns the conversation mode. Build the approver from the
        # persisted value so resuming an ASK/PLAN conversation cannot silently
        # fall back to the process default mode.
        try:
            persisted_mode = Mode(str(json.loads(resume.read_text(encoding="utf-8")).get("mode", mode.value)))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            persisted_mode = mode
        approver = None if args.event_json and persisted_mode == Mode.ASK and not args.auto_approve else _make_approver(persisted_mode, args.auto_approve, sys.stdin)
        conversation = Conversation.load(
            resume, settings=settings, model_client=client, workspace=workspace,
            trace=trace, event_sink=event_sink, approver=approver,
            stream_sink=emitter.stream if emitter is not None else None,
            compactor=_make_compactor(settings, client),
        )
        print("Resumed saved conversation from " + str(resume) + ".", file=sys.stderr)
        first = False
    else:
        conversation = Conversation(
            settings=settings, model_client=client, workspace=workspace,
            trace=trace, event_sink=event_sink, mode=mode, approver=approver,
            stream_sink=emitter.stream if emitter is not None else None,
            compactor=_make_compactor(settings, client),
        )
        first = True
    save = getattr(args, "save", None)
    print("SEECODER chat ready. Type a task line to send with mode=" + mode.value + ".", file=sys.stderr)
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            if emitter is not None:
                emitter.begin_run()
            outcome = conversation.start(text) if first else conversation.send(text)
        except Exception as error:  # Keep the long-lived chat process usable after one bad turn.
            final_text = f"Local turn failed: {type(error).__name__}: {error}"
            if emitter is not None:
                emitter.emit("turn_outcome", {"state": RunState.FAILED_PROTOCOL, "final_text": final_text,
                                               "steps": 0, "mode": conversation.current_mode.value,
                                               "recoverable": True})
            else:
                print(final_text, file=sys.stderr, flush=True)
            if save is not None:
                conversation.save(save)
            first = False
            continue
        first = False
        if save is not None:
            conversation.save(save)
        if emitter is not None:
            emitter.emit("turn_outcome", _outcome_data(outcome))
        else:
            _print_outcome(outcome, quiet=args.quiet)
        if outcome.state == RunState.PLAN_PROPOSED and not args.auto_approve:
            prompt = "Approve the proposed plan and execute it? (y/N): "
            if emitter is not None:
                emitter.emit("plan_approval_request", {"message": "Plan mode produced a mutation plan. Approve it to execute locally."})
            if sys.stdin.isatty():
                reply = input(prompt)
            else:
                print(prompt, file=sys.stderr, end="")
                reply = sys.stdin.readline()
            if reply.strip().lower().startswith("y"):
                executed = conversation.approve_plan()
                # Persist the post-approval continuation as a distinct turn.
                # Without this save, a desktop restart after approving a plan
                # would restore the pre-approval snapshot and replay the plan.
                if save is not None:
                    conversation.save(save)
                if emitter is not None:
                    emitter.emit("turn_outcome", _outcome_data(executed))
                else:
                    _print_outcome(executed, quiet=args.quiet)
            else:
                conversation.cancel_plan()
        while outcome.state == RunState.AWAITING_APPROVAL:
            prompt = "Approve the requested local action? (y/N): "
            if sys.stdin.isatty():
                reply = input(prompt)
            else:
                print(prompt, file=sys.stderr, end="")
                reply = sys.stdin.readline()
            outcome = conversation.resolve_approval(reply.strip().lower().startswith("y"))
            if save is not None:
                conversation.save(save)
            if emitter is not None:
                emitter.emit("turn_outcome", _outcome_data(outcome))
            else:
                _print_outcome(outcome, quiet=args.quiet)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.subcommand == "rollback-changeset":
        workspace = args.workspace.expanduser().resolve()
        journal_dir = args.journal_dir.expanduser().resolve()
        if not workspace.is_dir():
            print(json.dumps({"ok": False, "error": "Workspace is not an existing directory."}, ensure_ascii=False))
            return 2
        result = ChangeSetJournal(workspace, journal_dir).rollback(args.changeset_id)
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        return 0 if result.get("ok") else 2
    if args.subcommand not in {"run", "chat"}:
        return 2
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        print(f"Configuration error: workspace is not an existing directory: {workspace}", file=sys.stderr)
        return 2
    launch_directory = Path.cwd().resolve()
    env_file = args.env_file.expanduser() if getattr(args, "env_file", None) else launch_directory / ".env"
    trace_directory = (args.trace_dir.expanduser() if getattr(args, "trace_dir", None) else launch_directory / "runs").resolve()
    if _is_inside(env_file, workspace):
        print("Configuration error: --env-file must be outside the editable workspace", file=sys.stderr)
        return 2
    if _is_inside(trace_directory, workspace):
        print("Configuration error: --trace-dir must be outside the editable workspace", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_environment(
            env_file=env_file,
            max_steps=getattr(args, "max_steps", None),
            allow_dangerous_commands=getattr(args, "allow_dangerous_commands", False),
            execution_mode="host_shell" if getattr(args, "host_shell", False) else None,
        )
        if settings.execution_mode == "host_shell":
            print("Warning: host-shell mode is not an operating-system sandbox.", file=sys.stderr)
        trace = TraceWriter(trace_directory, secrets=(settings.api_key,))
        if args.subcommand == "run":
            return _run_run(args, settings, trace, workspace)
        return _run_chat(args, settings, trace, workspace)
    except (ConfigError, ValueError) as error:
        if getattr(args, "event_json", False):
            emitter = EventJsonEmitter(getattr(args, "session_id", None))
            emitter.begin_run()
            emitter.emit("configuration_error", {"message": str(error)})
        else:
            print(f"Configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
