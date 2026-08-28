"""Read-only visibility into workspace-local SEECODER skill packages."""

from __future__ import annotations

from typing import Any

from seecoder.skills import discover_workspace_skills
from seecoder.tools.base import WorkspaceBoundary
from seecoder.types import ToolResult


class ListSkillsTool:
    """Show optional local skills that are already bounded by the skill loader."""

    name = "list_skills"
    description = "List locally installed project skills under .seecoder/skills/<name>/SKILL.md. This never loads remote services."
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self.boundary = boundary

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if arguments:
            raise ValueError("'list_skills' does not accept arguments")
        skills = discover_workspace_skills(self.boundary.root)
        return ToolResult.success(
            {
                "skills": [
                    {"name": skill.name, "path": skill.path, "truncated": skill.truncated}
                    for skill in skills
                ],
                "install_path": ".seecoder/skills/<skill-name>/SKILL.md",
            }
        )
