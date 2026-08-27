"""Project memory: load an existing SEECODER.md / AGENTS.md into the agent context.

The agent may read or write this file with the regular local tools (it lives inside
the workspace), so the memory convention is purely additive: it is pinned into the
system prompt at conversation start so the model always sees the project's notes.
"""

from __future__ import annotations

from pathlib import Path

MEMORY_FILENAMES = ("SEECODER.md", "AGENTS.md")
DEFAULT_MAX_CHARS = 8_000


def find_memory_file(workspace: Path) -> Path | None:
    """Return the first memory file inside the workspace, if any."""

    for name in MEMORY_FILENAMES:
        candidate = workspace / name
        if candidate.is_file():
            return candidate
    return None


def load_memory_block(workspace: Path, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Read the project memory into a bounded, injectable context block."""

    path = find_memory_file(workspace)
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[memory truncated]"
    return f"<project_memory>\n{text}\n</project_memory>"
