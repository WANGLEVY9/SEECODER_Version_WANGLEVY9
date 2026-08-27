"""Deterministic, provider-neutral context budgeting."""

from __future__ import annotations

from dataclasses import replace

from seecoder.types import ChatMessage


class ContextBudgetExceeded(ValueError):
    """Raised when protocol-required history cannot fit without unsafe pruning."""


def estimate_message_chars(message: ChatMessage) -> int:
    """Use a conservative character budget without depending on a tokenizer."""

    tool_chars = sum(len(call.name) + len(call.arguments) + len(call.id) for call in message.tool_calls)
    return (
        len(message.role)
        + len(message.content or "")
        + len(message.tool_call_id or "")
        + len(message.reasoning_content or "")
        + tool_chars
        + 48
    )


def _turns(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    """Group an assistant tool request and its tool outputs as an inseparable turn."""

    result: list[list[ChatMessage]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            turn = [message]
            index += 1
            while index < len(messages) and messages[index].role == "tool":
                turn.append(messages[index])
                index += 1
            result.append(turn)
            continue
        result.append([message])
        index += 1
    return result


def _truncate_content(message: ChatMessage, limit: int) -> ChatMessage:
    if message.content is None or len(message.content) <= limit:
        return message
    if limit < 80:
        return replace(message, content=message.content[:limit])
    head = max(1, int(limit * 0.7))
    tail = max(1, limit - head - 47)
    return replace(
        message,
        content=(
            message.content[:head]
            + "\n...[content truncated by SEECODER context manager]...\n"
            + message.content[-tail:]
        ),
    )


class ContextManager:
    """Retain task intent and recent complete turns under a fixed character budget."""

    def __init__(self, char_budget: int) -> None:
        if char_budget < 2_000:
            raise ValueError("Context character budget must be at least 2,000")
        self.char_budget = char_budget

    def prepare(
        self, messages: list[ChatMessage], *, preserve_complete_history: bool = False
    ) -> list[ChatMessage]:
        total_size = sum(estimate_message_chars(item) for item in messages)
        if total_size <= self.char_budget:
            return list(messages)
        if preserve_complete_history:
            raise ContextBudgetExceeded(
                "Thinking-mode tool history exceeds the context budget; refusing to drop required reasoning content"
            )

        pinned: list[ChatMessage] = []
        remaining = list(messages)
        if remaining and remaining[0].role == "system":
            pinned.append(remaining.pop(0))
        if remaining and remaining[0].role == "user":
            pinned.append(remaining.pop(0))

        # A pasted task can itself exceed the model budget. Retain both the system
        # contract and task identity rather than silently exceeding the configured
        # ceiling or dropping either message.
        pinned_size = sum(estimate_message_chars(item) for item in pinned)
        if pinned_size > self.char_budget:
            fixed_overhead = sum(estimate_message_chars(replace(item, content="")) for item in pinned)
            content_budget = max(512, self.char_budget - fixed_overhead)
            if len(pinned) == 2:
                system_limit = min(len(pinned[0].content or ""), max(256, content_budget // 3))
                user_limit = max(256, content_budget - system_limit)
                pinned = [_truncate_content(pinned[0], system_limit), _truncate_content(pinned[1], user_limit)]
            else:
                pinned = [_truncate_content(item, content_budget) for item in pinned]

        # Preserve complete recent turns. Keeping tool request and result together
        # maintains the causal structure required by native tool-calling APIs.
        budget_left = max(0, self.char_budget - sum(estimate_message_chars(item) for item in pinned))
        chosen: list[list[ChatMessage]] = []
        for turn in reversed(_turns(remaining)):
            size = sum(estimate_message_chars(item) for item in turn)
            if size <= budget_left:
                chosen.append(turn)
                budget_left -= size
            elif not chosen:
                # Always preserve some evidence of the most recent turn, even when a
                # model has emitted unusually large content.
                compact = [_truncate_content(item, max(256, budget_left // max(1, len(turn)))) for item in turn]
                chosen.append(compact)
                break

        selected = [message for turn in reversed(chosen) for message in turn]
        return pinned + selected
