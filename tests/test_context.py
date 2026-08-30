from __future__ import annotations

import unittest

from seecoder.context import ContextBudgetExceeded, ContextManager, estimate_message_chars
from seecoder.types import ChatMessage, ToolCall


class ContextManagerTests(unittest.TestCase):
    def test_preserves_initial_task_and_latest_complete_tool_turn(self) -> None:
        manager = ContextManager(2_100)
        old_call = ToolCall(id="old", name="read_file", arguments='{"path":"old.py"}')
        recent_call = ToolCall(id="new", name="read_file", arguments='{"path":"new.py"}')
        messages = [
            ChatMessage(role="system", content="system"),
            ChatMessage(role="user", content="original task"),
            ChatMessage(role="assistant", content="old reasoning", tool_calls=(old_call,)),
            ChatMessage(role="tool", content="x" * 1_400, tool_call_id="old"),
            ChatMessage(role="assistant", content="recent reasoning", tool_calls=(recent_call,)),
            ChatMessage(role="tool", content="y" * 700, tool_call_id="new"),
        ]

        prepared = manager.prepare(messages)
        self.assertEqual(prepared[0].content, "system")
        self.assertEqual(prepared[1].content, "original task")
        self.assertTrue(any(message.tool_call_id == "new" for message in prepared))
        self.assertFalse(any(message.tool_call_id == "old" for message in prepared))

    def test_oversized_user_task_is_bounded_without_dropping_task_message(self) -> None:
        manager = ContextManager(2_000)
        prepared = manager.prepare(
            [ChatMessage(role="system", content="system rule " * 50), ChatMessage(role="user", content="task " * 2_000)]
        )
        self.assertEqual([message.role for message in prepared], ["system", "user"])
        self.assertLessEqual(sum(estimate_message_chars(message) for message in prepared), 2_100)
        self.assertIn("truncated", prepared[1].content or "")

    def test_thinking_mode_refuses_to_drop_required_history(self) -> None:
        manager = ContextManager(2_000)
        messages = [
            ChatMessage(role="system", content="system"),
            ChatMessage(role="user", content="task"),
            ChatMessage(
                role="assistant",
                content=None,
                reasoning_content="reasoning " * 400,
                tool_calls=(ToolCall(id="call", name="list_files", arguments="{}"),),
            ),
            ChatMessage(role="tool", tool_call_id="call", content="result " * 400),
        ]
        with self.assertRaises(ContextBudgetExceeded):
            manager.prepare(messages, preserve_complete_history=True)

    def test_oversized_recent_tool_turn_never_exceeds_hard_budget(self) -> None:
        manager = ContextManager(2_000)
        call = ToolCall(id="call", name="write_file", arguments='{"path":"x.py","content":"' + "x" * 3_000 + '"}')
        messages = [
            ChatMessage(role="system", content="system"),
            ChatMessage(role="user", content="task"),
            ChatMessage(role="assistant", content="write this", tool_calls=(call,)),
            ChatMessage(role="tool", tool_call_id="call", content="result " * 500),
        ]
        prepared = manager.prepare(messages)
        self.assertLessEqual(sum(estimate_message_chars(message) for message in prepared), manager.char_budget)
        # Keeping a malformed fragment of a tool call would be worse than
        # omitting an oversized old turn.
        self.assertFalse(any(message.tool_call_id == "call" for message in prepared))
