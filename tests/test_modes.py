"""Tests for the plan/ask/auto mode gate, approval policy, and usage accounting."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from seecoder.approval import Policy, is_read_only
from seecoder.config import Settings
from seecoder.runner import AgentRunner
from seecoder.types import ApprovalDecision, ChatMessage, Mode, ModelResponse, RunState, ToolCall, Usage


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage], tools: list[dict[str, Any]]) -> ModelResponse:
        self.requests.append(messages)
        return self.responses.pop(0)


def call(identifier: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id=identifier, name=name, arguments=json.dumps(arguments))


class ApprovalPolicyTests(unittest.TestCase):
    def test_read_only_tools_run_in_every_mode(self) -> None:
        for mode in (Mode.AUTO, Mode.PLAN, Mode.ASK):
            self.assertTrue(is_read_only("list_files"))
            self.assertEqual(Policy(mode).decide("read_file"), ApprovalDecision.ALLOW)

    def test_auto_permits_mutations_without_prompt(self) -> None:
        self.assertEqual(Policy(Mode.AUTO).decide("write_file"), ApprovalDecision.ALLOW)

    def test_ask_requires_approval_for_mutations(self) -> None:
        self.assertEqual(Policy(Mode.ASK).decide("apply_patch"), ApprovalDecision.NEEDS_APPROVAL)

    def test_plan_blocks_mutations_for_proposal(self) -> None:
        self.assertEqual(Policy(Mode.PLAN).decide("run_command"), ApprovalDecision.DENY)


class ModeRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _settings(self, **overrides: Any) -> Settings:
        values: dict[str, Any] = {"api_key": "test-key", "model": "fake", "max_steps": 5}
        values.update(overrides)
        return Settings(**values)

    def test_plan_mode_proposes_without_executing_mutations(self) -> None:
        (self.workspace / "bug.txt").write_text("broken", encoding="utf-8")
        model = ScriptedModel(
            [
                ModelResponse(None, (call("read", "read_file", {"path": "bug.txt"}),)),
                ModelResponse(None, (call("write", "write_file", {"path": "bug.txt", "content": "fixed"}),)),
                ModelResponse("Plan: repair bug.txt by replacing its content."),
            ]
        )
        runner = AgentRunner.for_workspace(
            settings=self._settings(), model_client=model, workspace=self.workspace, mode=Mode.PLAN
        )
        outcome = runner.run("Repair bug.txt")
        self.assertEqual(outcome.state, RunState.PLAN_PROPOSED)
        self.assertEqual((self.workspace / "bug.txt").read_text(encoding="utf-8"), "broken")
        self.assertEqual(len(outcome.plan), 1)
        self.assertEqual(outcome.plan[0].tool, "write_file")

    def test_ask_mode_deny_does_not_execute_mutation(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse(None, (call("write", "write_file", {"path": "x.txt", "content": "hello"}),)),
                ModelResponse("I was not permitted to edit, so I stopped."),
            ]
        )
        runner = AgentRunner.for_workspace(
            settings=self._settings(), model_client=model, workspace=self.workspace,
            mode=Mode.ASK, approver=lambda _call: False,
        )
        outcome = runner.run("Write x.txt")
        self.assertEqual(outcome.state, RunState.FINAL)
        self.assertFalse((self.workspace / "x.txt").exists())

    def test_ask_mode_allow_executes_mutation(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse(None, (call("write", "write_file", {"path": "x.txt", "content": "hello"}),)),
                ModelResponse("Wrote x.txt."),
            ]
        )
        runner = AgentRunner.for_workspace(
            settings=self._settings(), model_client=model, workspace=self.workspace,
            mode=Mode.ASK, approver=lambda _call: True,
        )
        outcome = runner.run("Write x.txt")
        self.assertEqual(outcome.state, RunState.FINAL)
        self.assertEqual((self.workspace / "x.txt").read_text(encoding="utf-8"), "hello")

    def test_usage_is_accumulated_and_reported(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse("done", usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
            ]
        )
        runner = AgentRunner.for_workspace(settings=self._settings(), model_client=model, workspace=self.workspace)
        outcome = runner.run("Say done")
        self.assertEqual(outcome.state, RunState.FINAL)
        self.assertIsNotNone(outcome.usage)
        self.assertEqual(outcome.usage.total_tokens, 15)
