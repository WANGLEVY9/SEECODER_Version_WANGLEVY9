"""Tests for the interactive multi-turn Conversation and plan approval."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from seecoder.config import Settings
from seecoder.session import Conversation
from seecoder.types import Mode, ModelResponse, RunState, ToolCall


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[list[Any]] = []

    def complete(self, messages: list[Any], tools: list[dict[str, Any]]) -> ModelResponse:
        self.requests.append(messages)
        return self.responses.pop(0)


def call(identifier: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id=identifier, name=name, arguments=json.dumps(arguments))


class ConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.settings = Settings(api_key="test", model="fake", max_steps=5)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_multi_turn_accumulates_history_and_usage(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse("First answer."),
                ModelResponse("Second answer."),
            ]
        )
        conversation = Conversation(settings=self.settings, model_client=model, workspace=self.workspace)
        first = conversation.start("What is the workspace?")
        second = conversation.send("And now?")
        self.assertEqual(first.state, RunState.FINAL)
        self.assertEqual(second.state, RunState.FINAL)
        # Two turns share one conversation: system + 2 user + 2 assistant.
        self.assertEqual(len(conversation.messages), 5)
        self.assertEqual(conversation.total_steps, 2)

    def test_plan_approval_executes_previously_proposed_mutation(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse(None, (call("a", "write_file", {"path": "x.txt", "content": "hi"}),)),
                ModelResponse("Plan: create x.txt."),
                ModelResponse(None, (call("b", "write_file", {"path": "x.txt", "content": "hi"}),)),
                ModelResponse("Created x.txt."),
            ]
        )
        conversation = Conversation(
            settings=self.settings, model_client=model, workspace=self.workspace, mode=Mode.PLAN
        )
        planned = conversation.start("Create x.txt.")
        self.assertEqual(planned.state, RunState.PLAN_PROPOSED)
        self.assertEqual(planned.plan[0].tool, "write_file")
        # The file must NOT have been created during plan mode.
        self.assertFalse((self.workspace / "x.txt").exists())
        executed = conversation.approve_plan()
        self.assertEqual(executed.state, RunState.FINAL)
        self.assertEqual((self.workspace / "x.txt").read_text(encoding="utf-8"), "hi")
