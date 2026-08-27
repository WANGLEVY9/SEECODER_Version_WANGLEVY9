"""Workspace-confined file inspection and editing tools."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from seecoder.tools.base import WorkspaceBoundary
from seecoder.types import ToolResult


_SKIPPED_DIRECTORY_NAMES = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
_MAX_FILE_CHARS = 12_000
_MAX_FILE_BYTES = 1_000_000
_MAX_WRITE_CHARS = 500_000


def _required_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{name}' must be a non-empty string")
    return value


def _bounded_int(
    arguments: dict[str, Any], name: str, *, default: int, minimum: int, maximum: int
) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"'{name}' must be an integer between {minimum} and {maximum}")
    return value


def _is_sensitive_path(boundary: WorkspaceBoundary, path: Path) -> bool:
    """Do not expose a normal local dotenv credential file to a remote model."""

    relative_parts = path.resolve(strict=False).relative_to(boundary.root).parts
    return any(part == ".env" or (part.startswith(".env.") and part != ".env.example") for part in relative_parts)


def _atomic_write(destination: Path, content: str) -> None:
    """Replace one workspace file without exposing a partially written result."""

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, delete=False, prefix=".seecoder-"
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(destination)
    finally:
        if temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


class ListFilesTool:
    name = "list_files"
    description = "List files and directories below a path in the configured workspace."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative directory, default '.'"},
            "max_depth": {"type": "integer", "minimum": 0, "maximum": 8, "default": 2},
            "max_entries": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
        },
        "additionalProperties": False,
    }

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self.boundary = boundary

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ValueError("'path' must be a string")
        max_depth = _bounded_int(arguments, "max_depth", default=2, minimum=0, maximum=8)
        max_entries = _bounded_int(arguments, "max_entries", default=200, minimum=1, maximum=500)
        directory = self.boundary.resolve(raw_path)
        if not directory.exists():
            return ToolResult.failure("NotFound", f"Directory does not exist: {raw_path}")
        if not directory.is_dir():
            return ToolResult.failure("NotADirectory", f"Path is not a directory: {raw_path}")
        if _is_sensitive_path(self.boundary, directory):
            return ToolResult.failure("SensitivePath", "Listing dotenv configuration directories is not allowed")

        entries: list[dict[str, Any]] = []
        truncated = False
        for current_root, directory_names, file_names in os.walk(directory, followlinks=False):
            current = Path(current_root)
            depth = len(current.relative_to(directory).parts)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in _SKIPPED_DIRECTORY_NAMES
                and self._is_contained(current / name)
            )
            if depth >= max_depth:
                directory_names[:] = []

            for name in [*directory_names, *sorted(file_names)]:
                child = current / name
                if not self._is_contained(child) or _is_sensitive_path(self.boundary, child):
                    continue
                if len(entries) >= max_entries:
                    truncated = True
                    break
                try:
                    stat = child.lstat()
                except OSError:
                    continue
                entries.append(
                    {
                        "path": self.boundary.relative(child),
                        "type": "directory" if child.is_dir() else "file",
                        "size_bytes": stat.st_size,
                    }
                )
            if truncated:
                break

        return ToolResult.success(
            {"path": self.boundary.relative(directory), "entries": entries}, meta={"truncated": truncated}
        )

    def _is_contained(self, path: Path) -> bool:
        try:
            self.boundary.resolve(str(path))
        except ValueError:
            return False
        return True


class ReadFileTool:
    name = "read_file"
    description = "Read a UTF-8 text file from the configured workspace, optionally by line range."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative text-file path"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        max_chars: int = _MAX_FILE_CHARS,
        max_bytes: int = _MAX_FILE_BYTES,
    ) -> None:
        self.boundary = boundary
        self.max_chars = max_chars
        self.max_bytes = max_bytes

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raw_path = _required_string(arguments, "path")
        path = self.boundary.resolve(raw_path)
        if not path.exists():
            return ToolResult.failure("NotFound", f"File does not exist: {raw_path}")
        if not path.is_file():
            return ToolResult.failure("NotAFile", f"Path is not a regular file: {raw_path}")
        if _is_sensitive_path(self.boundary, path):
            return ToolResult.failure("SensitivePath", "Reading dotenv configuration files is not allowed")
        size_bytes = path.stat().st_size
        if size_bytes > self.max_bytes:
            return ToolResult.failure(
                "FileTooLarge",
                f"File exceeds the {self.max_bytes}-byte read limit",
                data={"path": self.boundary.relative(path), "size_bytes": size_bytes},
            )
        with path.open("rb") as handle:
            if b"\x00" in handle.read(8_192):
                return ToolResult.failure("BinaryFile", f"Refusing to read binary-looking file: {raw_path}")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return ToolResult.failure("EncodingError", f"File is not valid UTF-8 text: {raw_path}")

        start_line = arguments.get("start_line", 1)
        end_line = arguments.get("end_line", len(lines) or 1)
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise ValueError("'start_line' must be a positive integer")
        if isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < start_line:
            raise ValueError("'end_line' must be an integer no smaller than 'start_line'")

        selected = lines[start_line - 1 : end_line]
        rendered = "\n".join(f"{number}: {line}" for number, line in enumerate(selected, start=start_line))
        truncated = False
        if len(rendered) > self.max_chars:
            rendered = rendered[: self.max_chars] + "\n...[file output truncated]"
            truncated = True
        return ToolResult.success(
            {
                "path": self.boundary.relative(path),
                "content": rendered,
                "line_count": len(lines),
                "returned_start_line": start_line,
                "returned_end_line": min(end_line, len(lines)),
            },
            meta={"truncated": truncated},
        )


class WriteFileTool:
    name = "write_file"
    description = "Atomically create or replace a UTF-8 text file within the configured workspace."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative destination path"},
            "content": {"type": "string", "description": "Complete replacement file content"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, boundary: WorkspaceBoundary, *, max_chars: int = _MAX_WRITE_CHARS) -> None:
        self.boundary = boundary
        self.max_chars = max_chars

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raw_path = _required_string(arguments, "path")
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("'content' must be a string")
        if len(content) > self.max_chars:
            raise ValueError(f"'content' exceeds the {self.max_chars}-character write limit")

        destination = self.boundary.resolve(raw_path)
        if destination == self.boundary.root:
            return ToolResult.failure("InvalidPath", "Cannot replace the workspace directory")
        if _is_sensitive_path(self.boundary, destination):
            return ToolResult.failure("SensitivePath", "Writing dotenv configuration files is not allowed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after mkdir to defend against an existing symlink in a newly
        # traversed parent directory.
        destination = self.boundary.resolve(str(destination))
        existed = destination.exists()
        _atomic_write(destination, content)
        return ToolResult.success(
            {
                "path": self.boundary.relative(destination),
                "bytes_written": len(content.encode("utf-8")),
                "created": not existed,
            }
        )


class SearchFilesTool:
    name = "search_files"
    description = "Search UTF-8 workspace files for an exact text string and return bounded matching lines."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Exact text to search for"},
            "path": {"type": "string", "description": "Workspace-relative directory, default '.'"},
            "max_matches": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
            "max_files": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 1000},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, boundary: WorkspaceBoundary, *, max_file_bytes: int = _MAX_FILE_BYTES) -> None:
        self.boundary = boundary
        self.max_file_bytes = max_file_bytes

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        query = _required_string(arguments, "query")
        raw_path = arguments.get("path", ".")
        if not isinstance(raw_path, str):
            raise ValueError("'path' must be a string")
        max_matches = _bounded_int(arguments, "max_matches", default=100, minimum=1, maximum=200)
        max_files = _bounded_int(arguments, "max_files", default=1000, minimum=1, maximum=5000)
        directory = self.boundary.resolve(raw_path)
        if not directory.is_dir():
            return ToolResult.failure("NotADirectory", f"Path is not a directory: {raw_path}")
        if _is_sensitive_path(self.boundary, directory):
            return ToolResult.failure("SensitivePath", "Searching dotenv configuration directories is not allowed")

        matches: list[dict[str, Any]] = []
        scanned_files = 0
        truncated = False
        for current_root, directory_names, file_names in os.walk(directory, followlinks=False):
            current = Path(current_root)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in _SKIPPED_DIRECTORY_NAMES
                and self._is_safe_file_candidate(current / name)
            )
            for name in sorted(file_names):
                path = current / name
                if not self._is_safe_file_candidate(path) or not path.is_file():
                    continue
                try:
                    if path.stat().st_size > self.max_file_bytes:
                        continue
                    with path.open("rb") as handle:
                        if b"\x00" in handle.read(8_192):
                            continue
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                scanned_files += 1
                for line_number, line in enumerate(lines, start=1):
                    if query not in line:
                        continue
                    matches.append(
                        {
                            "path": self.boundary.relative(path),
                            "line": line_number,
                            "content": line[:500],
                        }
                    )
                    if len(matches) >= max_matches:
                        truncated = True
                        break
                if truncated or scanned_files >= max_files:
                    truncated = truncated or scanned_files >= max_files
                    break
            if truncated:
                break
        return ToolResult.success(
            {"query": query, "matches": matches, "scanned_files": scanned_files},
            meta={"truncated": truncated},
        )

    def _is_safe_file_candidate(self, path: Path) -> bool:
        try:
            resolved = self.boundary.resolve(str(path))
        except ValueError:
            return False
        return not _is_sensitive_path(self.boundary, resolved)


class ApplyPatchTool:
    name = "apply_patch"
    description = "Replace one exact text block in a UTF-8 workspace file after checking its occurrence count."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative existing text file"},
            "old_text": {"type": "string", "description": "Exact current text block to replace"},
            "new_text": {"type": "string", "description": "Replacement text block"},
            "expected_occurrences": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def __init__(self, boundary: WorkspaceBoundary, *, max_file_bytes: int = _MAX_FILE_BYTES) -> None:
        self.boundary = boundary
        self.max_file_bytes = max_file_bytes

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raw_path = _required_string(arguments, "path")
        old_text = _required_string(arguments, "old_text")
        new_text = arguments.get("new_text")
        if not isinstance(new_text, str):
            raise ValueError("'new_text' must be a string")
        expected_occurrences = _bounded_int(
            arguments, "expected_occurrences", default=1, minimum=1, maximum=20
        )
        path = self.boundary.resolve(raw_path)
        if not path.is_file():
            return ToolResult.failure("NotAFile", f"Path is not a regular file: {raw_path}")
        if _is_sensitive_path(self.boundary, path):
            return ToolResult.failure("SensitivePath", "Patching dotenv configuration files is not allowed")
        if path.stat().st_size > self.max_file_bytes:
            return ToolResult.failure("FileTooLarge", f"File exceeds the {self.max_file_bytes}-byte patch limit")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult.failure("EncodingError", f"File is not valid UTF-8 text: {raw_path}")
        occurrences = content.count(old_text)
        if occurrences != expected_occurrences:
            return ToolResult.failure(
                "PatchContextMismatch",
                f"Expected {expected_occurrences} exact occurrences of old_text, found {occurrences}",
                data={"path": self.boundary.relative(path), "occurrences": occurrences},
            )
        updated = content.replace(old_text, new_text, 1)
        _atomic_write(path, updated)
        return ToolResult.success(
            {
                "path": self.boundary.relative(path),
                "replaced_occurrences": 1,
                "bytes_written": len(updated.encode("utf-8")),
            }
        )
