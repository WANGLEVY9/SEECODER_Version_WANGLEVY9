"""Command-line entry point for a transparent single-task agent run."""

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
from seecoder.trace import TraceWriter
from seecoder.types import RunState


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
    return parser


def _event_printer(event: str, data: dict[str, Any]) -> None:
    if event == "model_request":
        print(f"[step {data['step']}] requesting model")
    elif event == "tool_dispatch":
        print(f"[step {data['step']}] dispatching {data['count']} tool call(s)")
    elif event == "tool_result":
        suffix = "ok" if data["ok"] else f"error={data['error']}"
        print(f"  - {data['name']}: {suffix}")


def _event_json_printer(event: str, data: dict[str, Any]) -> None:
    """Emit a deliberately small local protocol for the zero-dependency desktop UI."""

    print(json.dumps({"event": event, "data": data}, ensure_ascii=False), flush=True)


def _exit_code(state: RunState) -> int:
    return {
        RunState.FINAL: 0,
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.subcommand != "run":
        return 2
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        print(f"Configuration error: workspace is not an existing directory: {workspace}", file=sys.stderr)
        return 2
    # Credential and trace locations are deliberately derived from the launch
    # directory, not from the editable agent workspace. A local command tool is
    # not an OS sandbox, so placing secrets in its workspace would be unsafe.
    launch_directory = Path.cwd().resolve()
    env_file = args.env_file.expanduser() if args.env_file else launch_directory / ".env"
    trace_directory = (args.trace_dir.expanduser() if args.trace_dir else launch_directory / "runs").resolve()
    if _is_inside(env_file, workspace):
        print("Configuration error: --env-file must be outside the editable workspace", file=sys.stderr)
        return 2
    if _is_inside(trace_directory, workspace):
        print("Configuration error: --trace-dir must be outside the editable workspace", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_environment(
            env_file=env_file,
            max_steps=args.max_steps,
            allow_dangerous_commands=args.allow_dangerous_commands,
            execution_mode="host_shell" if args.host_shell else None,
        )
        if settings.execution_mode == "host_shell":
            print("Warning: host-shell mode is not an operating-system sandbox.", file=sys.stderr)
        trace = TraceWriter(trace_directory, secrets=(settings.api_key,))
        client = RetryingModelClient(OpenAICompatibleClient(settings), retries=settings.model_retries)
        runner = AgentRunner.for_workspace(
            settings=settings,
            model_client=client,
            workspace=workspace,
            trace=trace,
            event_sink=_event_json_printer if args.event_json else (None if args.quiet else _event_printer),
        )
        outcome = runner.run(args.task)
    except (ConfigError, ValueError) as error:
        if args.event_json:
            print(json.dumps({"event": "configuration_error", "data": {"message": str(error)}}, ensure_ascii=False))
        else:
            print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    if args.event_json:
        print(
            json.dumps(
                {
                    "event": "run_outcome",
                    "data": {
                        "state": outcome.state,
                        "final_text": outcome.final_text,
                        "steps": outcome.steps,
                        "trace_path": outcome.trace_path,
                    },
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"\n[{outcome.state}] {outcome.final_text}")
        if outcome.trace_path:
            print(f"Trace: {outcome.trace_path}")
    return _exit_code(outcome.state)


if __name__ == "__main__":
    raise SystemExit(main())
