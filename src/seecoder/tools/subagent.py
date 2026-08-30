"""A bounded sub-agent tool: launch a focused child agent and return its result."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from seecoder.types import ToolResult


class SpawnAgentTool:
    capability = "write"
    """Run a nested, bounded agent to complete a focused sub-task.

    The factory is supplied by the runner and internally disables further sub-agent
    spawning, so delegation cannot grow unbounded. The sub-agent runs in auto mode
    against the same workspace and model client.
    """

    name = "spawn_agent"
    description = (
        "Launch a focused sub-agent to complete a bounded sub-task and return its final "
        "result. Use it to split large or independent work (e.g. a review, a targeted fix, "
        "or a focused search) rather than doing everything in one long turn."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short label for the sub-agent (e.g. 'reviewer')"},
            "task": {"type": "string", "description": "The self-contained task for the sub-agent"},
            "max_steps": {"type": "integer", "minimum": 1, "maximum": 30, "default": 6},
        },
        "required": ["name", "task"],
        "additionalProperties": False,
    }

    def __init__(self, factory: Callable[[str, str, int], str]) -> None:
        self.factory = factory

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        name = arguments.get("name")
        task = arguments.get("task")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("'name' must be a non-empty string")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("'task' must be a non-empty string")
        max_steps = arguments.get("max_steps", 6)
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 30:
            raise ValueError("'max_steps' must be an integer between 1 and 30")
        try:
            result = self.factory(name.strip(), task.strip(), max_steps)
        except Exception as error:  # sub-agent failures remain observable, not fatal
            return ToolResult.failure("SubAgentError", f"{type(error).__name__}: {error}")
        return ToolResult.success({"name": name.strip(), "result": result})
