"""Command-line entry point for a transparent single-task or interactive agent run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from seecoder import __version__
from seecoder.config import ConfigError, Settings
from seecoder.model_client import OpenAICompatibleClient, RetryingModelClient
from seecoder.runner import AgentRunner
from seecoder.session import Conversation
from seecoder.trace import TraceWriter
from seecoder.types import Mode, RunState, ToolCall


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


def _exit_code(state: RunState) -> int:
    return {
        RunState.FINAL: 0,
        RunState.PLAN_PROPOSED: 0,
        RunState.AWAITING_APPROVAL: 0,
        RunState.STOP_MAX_STEPS: 3,
        RunState.STOP_TOOL_ERROR_LIMIT: 3,
        RunState.STOP_CONTEXT_BUDGET: 3,
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
    runner = AgentRunner.for_workspace(
        settings=settings,
        model_client=client,
        workspace=workspace,
        trace=trace,
        event_sink=_event_json_printer if args.event_json else (None if args.quiet else _event_printer),
        mode=mode,
        approver=approver,
        stream_sink=_token_json_printer if args.event_json else None,
        compactor=_make_compactor(settings, client),
    )
    outcome = runner.run(args.task)
    if args.event_json:
        print(json.dumps({"event": "run_outcome", "data": {
            "state": outcome.state, "final_text": outcome.final_text, "steps": outcome.steps,
            "trace_path": outcome.trace_path, "mode": outcome.mode.value,
            "plan": [step.__dict__ for step in outcome.plan],
            "usage": {"total_tokens": outcome.usage.total_tokens} if outcome.usage else {},
        }}, ensure_ascii=False))
    else:
        _print_outcome(outcome, quiet=args.quiet)
    return _exit_code(outcome.state)


def _run_chat(args: Any, settings: Settings, trace: TraceWriter, workspace: Path) -> int:
    mode = Mode(args.mode) if args.mode else settings.mode
    approver = _make_approver(mode, args.auto_approve, sys.stdin)
    client = RetryingModelClient(OpenAICompatibleClient(settings), retries=settings.model_retries)
    event_sink = _event_json_printer if args.event_json else (None if args.quiet else _event_printer)
    resume = getattr(args, "resume", None)
    if resume is not None:
        # The snapshot owns the conversation mode. Build the approver from the
        # persisted value so resuming an ASK/PLAN conversation cannot silently
        # fall back to the process default mode.
        try:
            persisted_mode = Mode(str(json.loads(resume.read_text(encoding="utf-8")).get("mode", mode.value)))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            persisted_mode = mode
        approver = _make_approver(persisted_mode, args.auto_approve, sys.stdin)
        conversation = Conversation.load(
            resume, settings=settings, model_client=client, workspace=workspace,
            trace=trace, event_sink=event_sink, approver=approver,
            stream_sink=_token_json_printer if args.event_json else None,
            compactor=_make_compactor(settings, client),
        )
        print("Resumed saved conversation from " + str(resume) + ".", file=sys.stderr)
        first = False
    else:
        conversation = Conversation(
            settings=settings, model_client=client, workspace=workspace,
            trace=trace, event_sink=event_sink, mode=mode, approver=approver,
            stream_sink=_token_json_printer if args.event_json else None,
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
            outcome = conversation.start(text) if first else conversation.send(text)
        except Exception as error:  # Keep the long-lived chat process usable after one bad turn.
            final_text = f"Local turn failed: {type(error).__name__}: {error}"
            if args.event_json:
                print(json.dumps({"event": "turn_outcome", "data": {
                    "state": RunState.FAILED_PROTOCOL,
                    "final_text": final_text,
                    "steps": 0,
                    "mode": conversation.current_mode.value,
                    "recoverable": True,
                }}, ensure_ascii=False), flush=True)
            else:
                print(final_text, file=sys.stderr, flush=True)
            if save is not None:
                conversation.save(save)
            first = False
            continue
        first = False
        if save is not None:
            conversation.save(save)
        if args.event_json:
            print(json.dumps({"event": "turn_outcome", "data": {
                "state": outcome.state, "final_text": outcome.final_text, "steps": outcome.steps,
                "mode": outcome.mode.value,
                "plan": [step.__dict__ for step in outcome.plan],
                "recoverable": outcome.state in {RunState.FAILED_MODEL, RunState.FAILED_PROTOCOL, RunState.STOP_CONTEXT_BUDGET},
            }}, ensure_ascii=False), flush=True)
        else:
            _print_outcome(outcome, quiet=args.quiet)
        if outcome.state == RunState.PLAN_PROPOSED and not args.auto_approve:
            prompt = "Approve the proposed plan and execute it? (y/N): "
            if args.event_json:
                print(json.dumps({"event": "plan_approval_request", "data": {
                    "message": "Plan mode produced a mutation plan. Approve it to execute locally.",
                }}, ensure_ascii=False), flush=True)
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
                if args.event_json:
                    print(json.dumps({"event": "turn_outcome", "data": {
                        "state": executed.state, "final_text": executed.final_text,
                        "steps": executed.steps, "mode": executed.mode.value,
                    }}, ensure_ascii=False), flush=True)
                else:
                    _print_outcome(executed, quiet=args.quiet)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
            print(json.dumps({"event": "configuration_error", "data": {"message": str(error)}}, ensure_ascii=False))
        else:
            print(f"Configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
