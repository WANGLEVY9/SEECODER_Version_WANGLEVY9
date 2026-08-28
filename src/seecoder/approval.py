"""Tool mutability classification and the permission policy for plan/ask/auto modes."""

from __future__ import annotations

from seecoder.types import ApprovalDecision, Mode


# Observation-only tools run in every mode without an approval prompt.
READ_ONLY_TOOLS = frozenset(
    {
        "list_files", "read_file", "search_files", "search_code", "git_diff", "git_status", "git_log",
        "list_skills", "web_search",
    }
)


def is_read_only(tool_name: str) -> bool:
    """Return True when a tool only observes workspace state."""

    return tool_name in READ_ONLY_TOOLS


class Policy:
    """Decide whether a tool call may run immediately, must pause, or is blocked by mode.

    plan mode is handled specially by the runner: mutating tools are never executed,
    they are captured as a proposed plan for human review instead of being denied.
    """

    def __init__(self, mode: Mode) -> None:
        self.mode = mode

    def decide(self, tool_name: str) -> ApprovalDecision:
        if is_read_only(tool_name):
            return ApprovalDecision.ALLOW
        if self.mode == Mode.AUTO:
            return ApprovalDecision.ALLOW
        if self.mode == Mode.ASK:
            return ApprovalDecision.NEEDS_APPROVAL
        if self.mode == Mode.PLAN:
            return ApprovalDecision.DENY
        return ApprovalDecision.ALLOW
