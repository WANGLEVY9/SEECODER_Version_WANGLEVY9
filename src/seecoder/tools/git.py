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


class GitStatusTool(GitDiffTool):
    """Expose branch and porcelain status without allowing Git mutations."""

    name = "git_status"
    description = "Show the current local Git branch and bounded working-tree status. Read-only."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"max_entries": {"type": "integer", "minimum": 1, "maximum": 200, "default": 80}},
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        max_entries = arguments.get("max_entries", 80)
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or not 1 <= max_entries <= 200:
            raise ValueError("'max_entries' must be an integer between 1 and 200")
        try:
            repository = self._run(["rev-parse", "--is-inside-work-tree"])
        except FileNotFoundError:
            return ToolResult.failure("GitUnavailable", "The local 'git' executable is not available")
        except subprocess.TimeoutExpired:
            return ToolResult.failure("GitTimeout", "Git repository check exceeded the 10-second limit")
        if repository.returncode != 0 or repository.stdout.strip() != "true":
            return ToolResult.failure("NotGitRepository", "The selected workspace is not inside a Git work tree")
        try:
            branch = self._run(["branch", "--show-current"])
            status = self._run(["status", "--short", "--untracked-files=all"])
        except subprocess.TimeoutExpired:
            return ToolResult.failure("GitTimeout", "Git status exceeded the 10-second limit")
        if branch.returncode != 0 or status.returncode != 0:
            return ToolResult.failure("GitError", (branch.stderr or status.stderr).strip() or "Git could not inspect status")
        lines = status.stdout.splitlines()
        return ToolResult.success(
            {"branch": branch.stdout.strip() or "(detached HEAD)", "status": lines[:max_entries]},
            meta={"truncated": len(lines) > max_entries},
        )


class GitLogTool(GitDiffTool):
    """Expose a bounded history summary through fixed Git argv."""

    name = "git_log"
    description = "Show a bounded, read-only local Git commit history for the workspace."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"max_commits": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}},
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        max_commits = arguments.get("max_commits", 10)
        if isinstance(max_commits, bool) or not isinstance(max_commits, int) or not 1 <= max_commits <= 30:
            raise ValueError("'max_commits' must be an integer between 1 and 30")
        try:
            repository = self._run(["rev-parse", "--is-inside-work-tree"])
        except FileNotFoundError:
            return ToolResult.failure("GitUnavailable", "The local 'git' executable is not available")
        except subprocess.TimeoutExpired:
            return ToolResult.failure("GitTimeout", "Git repository check exceeded the 10-second limit")
        if repository.returncode != 0 or repository.stdout.strip() != "true":
            return ToolResult.failure("NotGitRepository", "The selected workspace is not inside a Git work tree")
        try:
            history = self._run(
                ["log", "--no-ext-diff", "--no-color", "--format=%h%x09%s%x09%an%x09%aI", "-n", str(max_commits)]
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failure("GitTimeout", "Git log exceeded the 10-second limit")
        if history.returncode != 0:
            return ToolResult.failure("GitError", history.stderr.strip() or "Git could not inspect history")
        commits = []
        for line in history.stdout.splitlines():
            parts = line.split("\t", maxsplit=3)
            if len(parts) != 4:
                continue
            revision, subject, author, committed_at = parts
            commits.append({"revision": revision, "subject": subject, "author": author, "committed_at": committed_at})
        return ToolResult.success({"commits": commits})
