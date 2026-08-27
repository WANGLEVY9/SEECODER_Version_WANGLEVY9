"""Symbol definition search: a deterministic, offline code index for the agent."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from seecoder.tools.base import WorkspaceBoundary
from seecoder.types import ToolResult

_SKIPPED_DIRECTORY_NAMES = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".c", ".cpp",
    ".h", ".hpp", ".rb", ".php", ".cs", ".kt", ".swift", ".scala",
}
_RAW_README_LIMIT = 8_192
_MAX_FILE_BYTES = 1_000_000
_NAME_RE = re.compile(
    r"^\s*(?:async\s+)?(class|def|function|fun|func|struct|interface|type|trait)\s+([A-Za-z_][A-Za-z0-9_]*)"
)


def _required_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{name}' must be a non-empty string")
    return value


def _bounded_int(arguments: dict[str, Any], name: str, *, default: int, minimum: int, maximum: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"'{name}' must be an integer between {minimum} and {maximum}")
    return value


class SearchCodeTool:
    """Return workspace symbol definitions that match a query, bounded and deterministic."""

    name = "search_code"
    description = (
        "Search workspace source files for symbol definitions (classes, functions, methods, "
        "interfaces, structs) matching a query. Returns file, line, kind and a snippet of the definition."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Symbol or definition text to match"},
            "path": {"type": "string", "description": "Workspace-relative directory, default '.'"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
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
        max_results = _bounded_int(arguments, "max_results", default=50, minimum=1, maximum=200)
        directory = self.boundary.resolve(raw_path)
        if not directory.is_dir():
            return ToolResult.failure("NotADirectory", f"Path is not a directory: {raw_path}")

        needle = query.lower()
        matches: list[dict[str, Any]] = []
        scanned_files = 0
        truncated = False
        for current_root, directory_names, file_names in os.walk(directory, followlinks=False):
            current = Path(current_root)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in _SKIPPED_DIRECTORY_NAMES and self._is_inside(current / name)
            )
            for name in sorted(file_names):
                path = current / name
                if path.suffix.lower() not in _CODE_EXTENSIONS or not path.is_file() or not self._is_inside(path):
                    continue
                try:
                    if path.stat().st_size > self.max_file_bytes:
                        continue
                    with path.open("rb") as handle:
                        if b"\x00" in handle.read(_RAW_README_LIMIT):
                            continue
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                scanned_files += 1
                for line_number, line in enumerate(lines, start=1):
                    match = _NAME_RE.match(line)
                    if not match:
                        continue
                    kind = match.group(1)
                    symbol = match.group(2)
                    if needle not in symbol.lower() and needle not in line.lower():
                        continue
                    matches.append({
                        "path": self.boundary.relative(path),
                        "line": line_number,
                        "symbol": symbol,
                        "kind": kind,
                        "snippet": line.strip()[:200],
                    })
                    if len(matches) >= max_results:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break
        return ToolResult.success(
            {"query": query, "matches": matches, "scanned_files": scanned_files},
            meta={"truncated": truncated},
        )

    def _is_inside(self, path: Path) -> bool:
        try:
            self.boundary.resolve(str(path))
        except ValueError:
            return False
        return True
