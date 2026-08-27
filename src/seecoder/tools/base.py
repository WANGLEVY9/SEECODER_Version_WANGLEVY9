"""Tool registration, JSON argument validation, and workspace boundary checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from seecoder.types import ToolCall, ToolResult


class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]

    def execute(self, arguments: dict[str, Any]) -> ToolResult: ...


class WorkspaceBoundary:
    """Resolve paths after following existing symlinks and keep them under root."""

    def __init__(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Workspace is not an existing directory: {root}")
        self.root = resolved

    def resolve(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("path must be a non-empty string")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise ValueError("path escapes the configured workspace") from error
        return resolved

    def relative(self, path: Path) -> str:
        return path.resolve(strict=False).relative_to(self.root).as_posix() or "."


@dataclass(slots=True)
class ToolRegistry:
    _tools: dict[str, Tool]

    @classmethod
    def create(cls, tools: list[Tool]) -> ToolRegistry:
        mapped: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in mapped:
                raise ValueError(f"Duplicate tool registration: {tool.name}")
            mapped[tool.name] = tool
        return cls(mapped)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def dispatch(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult.failure("UnknownTool", f"No local tool named '{call.name}' is registered")
        try:
            arguments = json.loads(call.arguments)
        except json.JSONDecodeError as error:
            return ToolResult.failure("InvalidArguments", f"Tool arguments are not valid JSON: {error.msg}")
        if not isinstance(arguments, dict):
            return ToolResult.failure("InvalidArguments", "Tool arguments must decode to a JSON object")
        try:
            return tool.execute(arguments)
        except (TypeError, ValueError) as error:
            return ToolResult.failure("ValidationError", str(error))
        except OSError as error:
            return ToolResult.failure("FilesystemError", str(error))
        except Exception as error:  # Defensive agent boundary; unexpected bugs remain observable.
            return ToolResult.failure("InternalToolError", f"{type(error).__name__}: {error}")
