"""Tool registration, JSON argument validation, and workspace boundary checks."""

from __future__ import annotations

import json
from difflib import get_close_matches
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from seecoder.types import ToolCall, ToolResult


class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    capability: str

    def execute(self, arguments: dict[str, Any]) -> ToolResult: ...


class WorkspaceBoundary:
    """Resolve paths after following existing symlinks and keep them under root."""

    def __init__(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Workspace is not an existing directory: {root}")
        self.root = resolved

    def update_root(self, root: Path) -> None:
        """Move the active boundary to a newly renamed workspace root.

        All built-in tools share one boundary instance. Updating it after a
        root rename keeps subsequent reads, writes, commands, and Git calls
        scoped to the new path for the remainder of the conversation.
        """

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

    def is_read_only(self, tool_name: str) -> bool:
        """Return the capability declared by a tool, without a global name list."""

        tool = self._tools.get(tool_name)
        return bool(tool is not None and getattr(tool, "capability", "write") == "read")

    def dispatch(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            available = sorted(self._tools)
            suggestion = get_close_matches(call.name, available, n=1, cutoff=0.55)
            hint = f" Did you mean '{suggestion[0]}'?" if suggestion else ""
            return ToolResult.failure(
                "UnknownTool",
                f"No local tool named '{call.name}' is registered.{hint} Available tools: {', '.join(available)}",
            )
        try:
            arguments = json.loads(call.arguments)
        except json.JSONDecodeError as error:
            return ToolResult.failure("InvalidArguments", f"Tool arguments are not valid JSON: {error.msg}")
        if not isinstance(arguments, dict):
            return ToolResult.failure("InvalidArguments", "Tool arguments must decode to a JSON object")
        schema_error = _validate_schema(arguments, tool.parameters)
        if schema_error is not None:
            return ToolResult.failure("InvalidArguments", schema_error)
        try:
            return tool.execute(arguments)
        except (TypeError, ValueError) as error:
            return ToolResult.failure("ValidationError", str(error))
        except OSError as error:
            return ToolResult.failure("FilesystemError", str(error))
        except Exception as error:  # Defensive agent boundary; unexpected bugs remain observable.
            return ToolResult.failure("InternalToolError", f"{type(error).__name__}: {error}")


def _validate_schema(value: Any, schema: dict[str, Any], *, path: str = "arguments") -> str | None:
    """Validate the small JSON-Schema subset used by local tools.

    This deliberately avoids a runtime schema dependency while enforcing the
    keywords that protect the built-in tools: object properties, required fields,
    additionalProperties, primitive types, arrays, enums, and numeric bounds.
    """

    expected = schema.get("type")
    type_ok = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected in type_ok and not type_ok[expected]:
        return f"{path} must be {expected}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {schema['enum']}"
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{path} must contain at least {schema['minLength']} characters"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path} must contain at most {schema['maxLength']} characters"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} must be at least {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} must be at most {schema['maximum']}"
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path} must contain at least {schema['minItems']} item(s)"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path} must contain at most {schema['maxItems']} item(s)"
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}.{key} is required"
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                return f"{path} contains unknown field(s): {', '.join(unknown)}"
        for key, item in value.items():
            if key in properties:
                error = _validate_schema(item, properties[key], path=f"{path}.{key}")
                if error is not None:
                    return error
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            error = _validate_schema(item, schema["items"], path=f"{path}[{index}]")
            if error is not None:
                return error
    return None
