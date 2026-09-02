"""A bounded local shell tool with timeout and credential scrubbing."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from shlex import join as shell_join
from typing import Any

from seecoder.tools.base import WorkspaceBoundary
from seecoder.types import ToolResult


_DANGEROUS_COMMANDS = (
    re.compile(r"(?:^|[;&|]\s*)rm\s+[^\n]*(?:-[^\n]*[rR]|--recursive)", re.IGNORECASE),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\s+-[^\n]*[fdx]", re.IGNORECASE),
    re.compile(r"(?:^|\s)(?:sudo|shutdown|reboot|mkfs)\b", re.IGNORECASE),
    re.compile(r"(?:^|[\s/])\.env(?:\.[A-Za-z0-9_-]+)?(?:\s|$)", re.IGNORECASE),
)
_SENSITIVE_ENV_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION", "CREDENTIAL")
_RESTRICTED_META_CHARACTERS = frozenset(";|&<>`$()\n\r")
_RESTRICTED_COMMANDS = frozenset({
    "python", "python3", "python3.12", "pytest", "ruff", "black", "git",
    "node", "npm", "pnpm", "yarn", "bun", "cargo", "go", "swift", "swiftc", "dotnet",
    "java", "javac", "mvn", "gradle",
})
_PYTHON_MODULES = frozenset({"unittest", "pytest", "compileall", "ruff"})
_GIT_READ_ONLY_SUBCOMMANDS = frozenset({"diff", "status", "log", "show", "ls-files", "rev-parse"})
_PACKAGE_SCRIPT_NAMES = frozenset({"test", "lint", "build", "check", "typecheck", "format", "fmt"})
_VERSION_PROBE_FLAGS = frozenset({"--version", "-version", "-v", "-V", "version"})
_MAVEN_GOALS = frozenset({"test", "verify", "package", "compile"})
_GRADLE_TASKS = frozenset({"test", "check", "build", "classes", "assemble"})

_HOST_SHELL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Shell command to run in the workspace"},
        "timeout_s": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30},
    },
    "required": ["command"],
    "additionalProperties": False,
}
_RESTRICTED_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 32,
            "description": "Allowed project test/build/format/read-only-Git command as a literal argument array",
        },
        "timeout_s": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30},
    },
    "required": ["argv"],
    "additionalProperties": False,
}


class _BoundedCapture:
    """Continuously drain a pipe while retaining only a bounded leading prefix."""

    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        self._data = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        remaining = self.maximum_bytes - len(self._data)
        if remaining > 0:
            self._data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True

    def text(self) -> str:
        suffix = "\n...[output truncated]" if self.truncated else ""
        return self._data.decode("utf-8", errors="replace") + suffix


def _drain(stream: Any, capture: _BoundedCapture) -> None:
    try:
        for chunk in iter(lambda: stream.read(16_384), b""):
            capture.append(chunk)
    finally:
        stream.close()


class RunCommandTool:
    capability = "command"
    name = "run_command"
    description = (
        "Run a bounded local command and return bounded stdout/stderr. In restricted mode, pass a literal "
        "argv array only: safe tool-version probes and project validation/build commands are allowed; package "
        "installation, arbitrary shell text, and destructive commands are blocked."
    )
    parameters = _HOST_SHELL_PARAMETERS

    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        default_timeout_s: int = 30,
        max_timeout_s: int = 120,
        allow_dangerous_commands: bool = False,
        max_output_chars: int = 8_000,
        execution_mode: str = "host_shell",
    ) -> None:
        self.boundary = boundary
        self.default_timeout_s = default_timeout_s
        self.max_timeout_s = max_timeout_s
        self.allow_dangerous_commands = allow_dangerous_commands
        self.max_output_chars = max_output_chars
        if execution_mode not in {"restricted", "host_shell"}:
            raise ValueError("execution_mode must be restricted or host_shell")
        self.execution_mode = execution_mode
        self.parameters = _RESTRICTED_PARAMETERS if execution_mode == "restricted" else _HOST_SHELL_PARAMETERS

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        timeout_s = arguments.get("timeout_s", self.default_timeout_s)
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or not 1 <= timeout_s <= self.max_timeout_s:
            raise ValueError(f"'timeout_s' must be an integer between 1 and {self.max_timeout_s}")
        if self.execution_mode == "restricted":
            argv_or_error = self._restricted_argv(arguments)
            if isinstance(argv_or_error, ToolResult):
                return argv_or_error
            return self._execute(argv_or_error, timeout_s=timeout_s, shell=False, display=shell_join(argv_or_error))

        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("'command' must be a non-empty string")
        if not self.allow_dangerous_commands and any(pattern.search(command) for pattern in _DANGEROUS_COMMANDS):
            return ToolResult.failure(
                "DangerousCommand",
                "Command matches the P0 destructive-command guard. Re-run only with explicit --allow-dangerous-commands.",
            )
        return self._execute(command, timeout_s=timeout_s, shell=True, display=command)

    def _execute(self, invocation: str | list[str], *, timeout_s: int, shell: bool, display: str) -> ToolResult:

        started = time.monotonic()
        try:
            process = subprocess.Popen(
                invocation,
                cwd=self.boundary.root,
                shell=shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._sanitized_environment(),
                start_new_session=os.name != "nt",
            )
        except FileNotFoundError:
            # A missing compiler or package manager is useful environment
            # evidence, not an AgentRunner fault. Model code already treats a
            # non-zero exit as a signal to choose another validation path.
            return ToolResult.success(
                {
                    "command": display,
                    "exit_code": 127,
                    "stdout": "",
                    "stderr": "Executable was not found in PATH.",
                    "duration_ms": round((time.monotonic() - started) * 1_000),
                },
                meta={"unavailable": True},
            )
        assert process.stdout is not None and process.stderr is not None
        stdout_capture = _BoundedCapture(self.max_output_chars)
        stderr_capture = _BoundedCapture(self.max_output_chars)
        stdout_thread = threading.Thread(target=_drain, args=(process.stdout, stdout_capture), daemon=True)
        stderr_thread = threading.Thread(target=_drain, args=(process.stderr, stderr_capture), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_process_group(process)
            process.wait()
        except KeyboardInterrupt:
            self._terminate_process_group(process)
            process.wait()
            raise
        finally:
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
        duration_ms = round((time.monotonic() - started) * 1_000)
        data = {
            "command": display,
            "exit_code": process.returncode,
            "stdout": stdout_capture.text(),
            "stderr": stderr_capture.text(),
            "duration_ms": duration_ms,
        }
        if timed_out:
            return ToolResult.failure(
                "CommandTimeout", f"Command exceeded its {timeout_s}-second timeout", data=data
            )
        return ToolResult.success(data, meta={"truncated": stdout_capture.truncated or stderr_capture.truncated})

    @staticmethod
    def _restricted_argv(arguments: dict[str, Any]) -> list[str] | ToolResult:
        argv = arguments.get("argv")
        if not isinstance(argv, list) or not 1 <= len(argv) <= 32 or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("'argv' must be a non-empty array of at most 32 non-empty strings")
        if any(
            any(character in item for character in _RESTRICTED_META_CHARACTERS)
            or item.startswith(("/", "~"))
            or "=/" in item
            or ".." in item.split("/")
            for item in argv
        ):
            return ToolResult.failure(
                "RestrictedCommand",
                "restricted mode rejects shell metacharacters, absolute paths, and parent-directory paths",
            )
        program = argv[0]
        if program not in _RESTRICTED_COMMANDS:
            return ToolResult.failure(
                "RestrictedCommand",
                f"'{program}' is not allowed in restricted mode. Use a dedicated local tool when available (for example delete_file for file cleanup).",
            )
        # Exact version probes are read-only and are required before choosing
        # a language-specific validation path. Keep this narrow: one program
        # and one flag, with no shell, paths, or extra arguments.
        if len(argv) == 2 and argv[1] in _VERSION_PROBE_FLAGS:
            return argv
        if program.startswith("python"):
            if len(argv) < 3 or argv[1] != "-m" or argv[2] not in _PYTHON_MODULES:
                return ToolResult.failure(
                    "RestrictedCommand",
                    "restricted Python commands must use '-m unittest|pytest|compileall|ruff'",
                )
        elif program == "git":
            if len(argv) < 2 or argv[1] not in _GIT_READ_ONLY_SUBCOMMANDS:
                return ToolResult.failure("RestrictedCommand", "only read-only Git subcommands are allowed")
        elif program in {"npm", "pnpm", "yarn", "bun"}:
            if not _valid_package_command(argv):
                return ToolResult.failure("RestrictedCommand", "package-manager commands are limited to test, lint, build, check, typecheck, or format scripts")
        elif program == "mvn":
            if len(argv) < 2 or argv[1] not in _MAVEN_GOALS:
                return ToolResult.failure("RestrictedCommand", "mvn is limited to compile, test, verify, and package")
        elif program == "gradle":
            if len(argv) < 2 or argv[1] not in _GRADLE_TASKS:
                return ToolResult.failure("RestrictedCommand", "gradle is limited to test, check, build, classes, and assemble")
        elif program == "cargo":
            if len(argv) < 2 or argv[1] not in {"test", "check", "build", "clippy", "fmt"}:
                return ToolResult.failure("RestrictedCommand", "cargo is limited to test, check, build, clippy, and fmt")
        elif program == "go":
            if len(argv) < 2 or argv[1] not in {"test", "build", "vet", "fmt"}:
                return ToolResult.failure("RestrictedCommand", "go is limited to test, build, vet, and fmt")
        elif program == "swift":
            if len(argv) < 2 or argv[1] not in {"test", "build", "package"}:
                return ToolResult.failure("RestrictedCommand", "swift is limited to test, build, and package")
        elif program == "swiftc":
            if len(argv) < 2 or argv[1] not in {"-typecheck", "-parse"}:
                return ToolResult.failure("RestrictedCommand", "swiftc is limited to -typecheck and -parse")
        elif program == "node":
            if len(argv) < 3 or argv[1] != "--check":
                return ToolResult.failure("RestrictedCommand", "node is limited to --check for syntax validation")
        elif program in {"java", "javac"}:
            return ToolResult.failure("RestrictedCommand", "java and javac are limited to one version-probe flag")
        elif program == "dotnet":
            if len(argv) < 2 or argv[1] not in {"test", "build"}:
                return ToolResult.failure("RestrictedCommand", "dotnet is limited to test and build")
        elif program == "ruff":
            if len(argv) < 2 or argv[1] not in {"check", "format"} or (argv[1] == "format" and "--check" not in argv):
                return ToolResult.failure("RestrictedCommand", "ruff is limited to check and format --check")
        elif program == "black":
            if "--check" not in argv:
                return ToolResult.failure("RestrictedCommand", "black is limited to --check")
        return argv

    @staticmethod
    def _sanitized_environment() -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in _SENSITIVE_ENV_MARKERS)
        }

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[str]) -> None:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=1)
                return
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return
        try:
            process.kill()
        except ProcessLookupError:
            return


def _valid_package_command(argv: list[str]) -> bool:
    """Allow common read/validation scripts while rejecting arbitrary package commands."""

    if len(argv) < 2:
        return False
    if argv[1] in _PACKAGE_SCRIPT_NAMES:
        return True
    if argv[1] in {"run", "exec"} and len(argv) >= 3:
        script = argv[2].split(":", 1)[0]
        return script in _PACKAGE_SCRIPT_NAMES
    return False
