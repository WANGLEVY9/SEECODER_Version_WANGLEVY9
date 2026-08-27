"""Read-only Git inspection tool with fixed argument vectors."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from seecoder.tools.base import WorkspaceBoundary
from seecoder.types import ToolResult


class GitDiffTool:
    """Expose bounded local change information without allowing Git mutations."""

    name = "git_diff"
    description = "Show bounded, read-only Git status and unstaged diff for a workspace path."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative path, default '.'"},
            "max_chars": {"type": "integer", "minimum": 500, "maximum": 20000, "default": 8000},
        },
        "additionalProperties": False,
    }

    def __init__(self, boundary: WorkspaceBoundary, *, timeout_s: int = 10) -> None:
        self.boundary = boundary
        self.timeout_s = timeout_s

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ValueError("'path' must be a string")
        max_chars = arguments.get("max_chars", 8_000)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 500 <= max_chars <= 20_000:
            raise ValueError("'max_chars' must be an integer between 500 and 20000")
        selected = self.boundary.resolve(raw_path)
        if not selected.exists():
            return ToolResult.failure("NotFound", f"Path does not exist: {raw_path}")
        relative = self.boundary.relative(selected)

        try:
            repository = self._run(["rev-parse", "--is-inside-work-tree"])
        except FileNotFoundError:
            return ToolResult.failure("GitUnavailable", "The local 'git' executable is not available")
        except subprocess.TimeoutExpired:
            return ToolResult.failure("GitTimeout", "Git repository check exceeded the 10-second limit")
        if repository.returncode != 0 or repository.stdout.strip() != "true":
            return ToolResult.failure("NotGitRepository", "The selected workspace is not inside a Git work tree")

        try:
            status = self._run(["status", "--short", "--", relative])
            diff = self._run(["diff", "--no-ext-diff", "--no-color", "--", relative])
        except subprocess.TimeoutExpired:
            return ToolResult.failure("GitTimeout", "Git diff exceeded the 10-second limit")
        if status.returncode != 0 or diff.returncode != 0:
            message = (status.stderr or diff.stderr).strip() or "Git could not inspect the selected path"
            return ToolResult.failure("GitError", message)

        rendered_status = status.stdout.strip()
        rendered_diff, truncated = _truncate(diff.stdout, max_chars)
        return ToolResult.success(
            {"path": relative, "status": rendered_status, "diff": rendered_diff},
            meta={"truncated": truncated},
        )

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION", "CREDENTIAL"))
        }
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
        return subprocess.run(
            ["git", "-C", str(self.boundary.root), *arguments],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_s,
            env=environment,
        )


def _truncate(value: str, maximum: int) -> tuple[str, bool]:
    if len(value) <= maximum:
        return value, False
    return value[:maximum] + "\n...[git diff truncated]", True
