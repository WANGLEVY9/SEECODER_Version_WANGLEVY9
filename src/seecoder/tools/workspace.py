"""Bounded workspace discovery helpers for common coding-agent workflows."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

from seecoder.tools.base import WorkspaceBoundary
from seecoder.tools.files import _SKIPPED_DIRECTORY_NAMES, _is_sensitive_path
from seecoder.types import ToolResult


class FindFilesTool:
    capability = "read"
    """Find workspace files by a path/name glob without invoking a shell."""

    name = "find_files"
    description = "Find bounded workspace file paths matching a glob such as **/*.py or test_*.py. Read-only."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob matched against workspace-relative paths"},
            "path": {"type": "string", "description": "Workspace-relative directory, default '.'"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self.boundary = boundary

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 120 or "\x00" in pattern:
            raise ValueError("'pattern' must be a non-empty glob of at most 120 characters")
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ValueError("'path' must be a string")
        max_results = arguments.get("max_results", 100)
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 500:
            raise ValueError("'max_results' must be an integer between 1 and 500")
        directory = self.boundary.resolve(raw_path)
        if not directory.is_dir():
            return ToolResult.failure("NotADirectory", f"Path is not a directory: {raw_path}")
        matches: list[str] = []
        truncated = False
        for current_root, directory_names, file_names in os.walk(directory, followlinks=False):
            current = Path(current_root)
            directory_names[:] = sorted(
                name for name in directory_names
                if name not in _SKIPPED_DIRECTORY_NAMES and self._safe(current / name)
            )
            for name in sorted(file_names):
                candidate = current / name
                if not self._safe(candidate) or _is_sensitive_path(self.boundary, candidate):
                    continue
                relative = self.boundary.relative(candidate)
                if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern):
                    matches.append(relative)
                    if len(matches) >= max_results:
                        truncated = True
                        break
            if truncated:
                break
        return ToolResult.success({"pattern": pattern, "matches": matches}, meta={"truncated": truncated})

    def _safe(self, path: Path) -> bool:
        try:
            return self.boundary.resolve(str(path)).is_file() or self.boundary.resolve(str(path)).is_dir()
        except ValueError:
            return False


class ProjectOverviewTool:
    capability = "read"
    """Return a non-content project inventory useful before planning edits."""

    name = "project_overview"
    description = "Summarize detected project manifests and bounded source-file extension counts. Read-only."
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self.boundary = boundary

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if arguments:
            raise ValueError("'project_overview' does not accept arguments")
        manifests = []
        extensions: dict[str, int] = {}
        scanned = 0
        known_manifests = {"pyproject.toml", "package.json", "Package.swift", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Makefile"}
        for current_root, directory_names, file_names in os.walk(self.boundary.root, followlinks=False):
            current = Path(current_root)
            directory_names[:] = [name for name in directory_names if name not in _SKIPPED_DIRECTORY_NAMES and self._safe(current / name)]
            for name in sorted(file_names):
                path = current / name
                if not self._safe(path) or _is_sensitive_path(self.boundary, path):
                    continue
                scanned += 1
                relative = self.boundary.relative(path)
                if name in known_manifests:
                    manifests.append(relative)
                suffix = path.suffix.lower()
                if suffix:
                    extensions[suffix] = extensions.get(suffix, 0) + 1
                if scanned >= 5_000:
                    return ToolResult.success({"manifests": manifests[:100], "files_by_extension": dict(sorted(extensions.items())), "scanned_files": scanned}, meta={"truncated": True})
        return ToolResult.success({"manifests": manifests[:100], "files_by_extension": dict(sorted(extensions.items())), "scanned_files": scanned})

    def _safe(self, path: Path) -> bool:
        try:
            self.boundary.resolve(str(path))
            return True
        except ValueError:
            return False
