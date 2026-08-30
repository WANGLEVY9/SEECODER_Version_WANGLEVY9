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


def _fit_message(message: ChatMessage, allowance: int) -> ChatMessage | None:
    """Return a safely shortened message that fits ``allowance`` exactly.

    Tool-call identifiers and arguments are protocol data, not prose: trimming
    them would make a later provider request invalid.  If that immutable shape
    alone does not fit, the caller must drop the whole (old) turn or stop the
    run rather than quietly violating the configured budget.
    """

    if estimate_message_chars(message) <= allowance:
        return message
    base = replace(message, content="", reasoning_content="")
    if estimate_message_chars(base) > allowance:
        return None
    source = message.content or ""
    # Reasoning content is retained only in thinking mode, where callers ask
    # for complete history and never reach this lossy path.  Outside it, prose
    # content is the useful and safe thing to retain.
    if not source:
        return base
    low, high = 0, len(source)
    best = base
    while low <= high:
        middle = (low + high) // 2
        candidate = _truncate_content(replace(message, reasoning_content=None), middle)
        if estimate_message_chars(candidate) <= allowance:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


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
            # Allocate the fixed system/task contract first, then shorten only
            # textual fields.  There is intentionally no ``max(256, ...)``:
            # that old lower bound was able to exceed the configured budget.
            compacted_pinned: list[ChatMessage] = []
            remaining_budget = self.char_budget
            for index, item in enumerate(pinned):
                remaining_items = len(pinned) - index
                fixed_rest = sum(
                    estimate_message_chars(replace(other, content="", reasoning_content=""))
                    for other in pinned[index + 1 :]
                )
                allowance = max(0, remaining_budget - fixed_rest)
                fitted = _fit_message(item, allowance)
                if fitted is None:
                    raise ContextBudgetExceeded(
                        "Required system/task message metadata exceeds the context budget."
                    )
                compacted_pinned.append(fitted)
                remaining_budget -= estimate_message_chars(fitted)
            pinned = compacted_pinned

        # Preserve complete recent turns. Keeping tool request and result together
        # maintains the causal structure required by native tool-calling APIs.
        budget_left = max(0, self.char_budget - sum(estimate_message_chars(item) for item in pinned))
        chosen: list[list[ChatMessage]] = []
        for turn in reversed(_turns(remaining)):
            size = sum(estimate_message_chars(item) for item in turn)
            if size <= budget_left:
                chosen.append(turn)
                budget_left -= size
            elif not chosen and len(turn) == 1:
                # A plain, oversized most-recent message can be shortened.  A
                # tool turn is indivisible: trimming tool-call JSON would break
                # the provider protocol, so omit that old turn instead.
                compact = _fit_message(turn[0], budget_left)
                if compact is not None:
                    chosen.append([compact])
                break

        selected = [message for turn in reversed(chosen) for message in turn]
        prepared = pinned + selected
        if sum(estimate_message_chars(item) for item in prepared) > self.char_budget:
            # This is a hard postcondition.  Keep the defensive error even
            # though the allocation above should make it unreachable.
            raise ContextBudgetExceeded("Context manager could not fit messages within the configured budget.")
        return prepared
