"""Optional model-driven context compaction when the message history overflows.

This is an additive, opt-in strategy (default off). When the full history exceeds the
character budget, the runner asks the model to compress the older turns (everything except
the system prompt, the initial task, and the most recent complete turns) into a durable
<compacted_context> note. If compaction fails or is disabled, the deterministic trim in
context.py still applies, so the agent never loses its task contract.
"""

from __future__ import annotations

from typing import Any

from seecoder.context import _turns
from seecoder.types import ChatMessage

DEFAULT_KEEP_TURNS = 2
SUMMARY_PROMPT = (
    "You are compressing an older portion of a coding agent conversation. "
    "Produce concise working notes (2-5 sentences) capturing: the goal, the files and "
    "code inspected, the decisions made, and any open questions. Do not repeat tool "
    "chatter; keep only durable context that still matters for the task."
)


def compactable_prefix(messages: list[ChatMessage], *, keep_turns: int = DEFAULT_KEEP_TURNS) -> list[ChatMessage]:
    """Return the older messages eligible for compaction, preserving recent complete turns."""

    groups = _turns(messages)
    if len(groups) <= keep_turns + 2:
        return []
    prefix = groups[2 : len(groups) - keep_turns]
    return [message for group in prefix for message in group]


def _render_prefix(prefix: list[ChatMessage], max_chars: int = 12_000) -> str:
    parts: list[str] = []
    for message in prefix:
        if message.role == "assistant":
            calls = [call.name for call in message.tool_calls]
            text = message.content or ""
            parts.append(f"assistant: {text}" + (f" [calls {calls}]" if calls else ""))
        elif message.role == "tool":
            parts.append(f"tool result: {message.content[:500] if message.content else ''}")
        else:
            parts.append(f"{message.role}: {message.content}")
    rendered = "\n".join(parts)
    return rendered[:max_chars]


def summarize(client: Any, prefix: list[ChatMessage]) -> str:
    """Ask the provider to emit a compact summary of the older turns."""

    if not prefix:
        return ""
    payload = [
        ChatMessage(role="system", content=SUMMARY_PROMPT),
        ChatMessage(role="user", content=_render_prefix(prefix)),
    ]
    try:
        response = client.complete(payload, [])
    except Exception:
        return ""
    return (response.content or "").strip()
