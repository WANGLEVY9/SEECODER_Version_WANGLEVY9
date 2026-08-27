from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from seecoder.config import Settings
from seecoder.model_client import ModelClientError
from seecoder.runner import AgentRunner
from seecoder.trace import TraceWriter
from seecoder.types import ChatMessage, ModelResponse, RunState, ToolCall


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage], tools: list[dict[str, Any]]) -> ModelResponse:  # noqa: ARG002
        self.requests.append(messages)
        return self.responses.pop(0)


class FailingModel:
    def complete(self, messages: list[ChatMessage], tools: list[dict[str, Any]]) -> ModelResponse:  # noqa: ARG002
        raise ModelClientError("provider unavailable", retryable=False)


def call(identifier: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id=identifier, name=name, arguments=json.dumps(arguments))


class AgentRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _settings(self, **overrides: Any) -> Settings:
        values: dict[str, Any] = {"api_key": "test-key", "model": "fake", "max_steps": 5}
        values.update(overrides)
        return Settings(**values)

    def test_full_read_write_command_loop(self) -> None:
        (self.workspace / "bug.txt").write_text("broken", encoding="utf-8")
        test_command = f"{sys.executable} -c \"from pathlib import Path; assert Path('bug.txt').read_text() == 'fixed'\""
        model = ScriptedModel(
            [
                ModelResponse(None, (call("read", "read_file", {"path": "bug.txt"}),)),
                ModelResponse(None, (call("write", "write_file", {"path": "bug.txt", "content": "fixed"}),)),
                ModelResponse(None, (call("test", "run_command", {"command": test_command}),)),
                ModelResponse("Fixed bug.txt and verified it with the supplied test."),
            ]
        )
        runner = AgentRunner.for_workspace(
            settings=self._settings(execution_mode="host_shell"), model_client=model, workspace=self.workspace
        )

        outcome = runner.run("Repair the text fixture and validate it.")

        self.assertEqual(outcome.state, RunState.FINAL)
        self.assertEqual(outcome.steps, 4)
        self.assertEqual((self.workspace / "bug.txt").read_text(encoding="utf-8"), "fixed")
        self.assertEqual(len(model.requests), 4)
        self.assertIn("verified", outcome.final_text)

    def test_repeated_tool_errors_stop_the_run(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse(None, (call("one", "unknown", {}),)),
                ModelResponse(None, (call("two", "unknown", {}),)),
            ]
        )
        runner = AgentRunner.for_workspace(
            settings=self._settings(max_consecutive_tool_errors=2), model_client=model, workspace=self.workspace
        )
        outcome = runner.run("Do something")
        self.assertEqual(outcome.state, RunState.STOP_TOOL_ERROR_LIMIT)

    def test_max_steps_is_a_named_stop_condition(self) -> None:
        response = ModelResponse(None, (call("list", "list_files", {"path": "."}),))
        runner = AgentRunner.for_workspace(
            settings=self._settings(max_steps=2), model_client=ScriptedModel([response, response]), workspace=self.workspace
        )
        outcome = runner.run("Keep investigating")
        self.assertEqual(outcome.state, RunState.STOP_MAX_STEPS)
        self.assertEqual(outcome.steps, 2)

    def test_model_failure_is_a_named_stop_condition(self) -> None:
        runner = AgentRunner.for_workspace(settings=self._settings(), model_client=FailingModel(), workspace=self.workspace)
        outcome = runner.run("Run a task")
        self.assertEqual(outcome.state, RunState.FAILED_MODEL)
        self.assertIn("provider unavailable", outcome.final_text)

    def test_thinking_tool_turn_preserves_reasoning_for_next_request(self) -> None:
        reasoning = "inspect the workspace before making a change"
        model = ScriptedModel(
            [
                ModelResponse(
                    None,
                    (call("list", "list_files", {"path": "."}),),
                    reasoning_content=reasoning,
                ),
                ModelResponse("Inspection complete.", reasoning_content="final reasoning"),
            ]
        )
        runner = AgentRunner.for_workspace(
            settings=self._settings(thinking_mode="enabled"), model_client=model, workspace=self.workspace
        )
        outcome = runner.run("Inspect the workspace")
        self.assertEqual(outcome.state, RunState.FINAL)
        assistant_messages = [message for message in model.requests[1] if message.role == "assistant"]
        self.assertEqual(assistant_messages[-1].reasoning_content, reasoning)

    def test_thinking_mode_refuses_missing_reasoning_on_tool_call(self) -> None:
        model = ScriptedModel([ModelResponse(None, (call("list", "list_files", {"path": "."}),))])
        runner = AgentRunner.for_workspace(
            settings=self._settings(thinking_mode="enabled"), model_client=model, workspace=self.workspace
        )
        outcome = runner.run("Inspect the workspace")
        self.assertEqual(outcome.state, RunState.FAILED_PROTOCOL)

    def test_thinking_context_overflow_has_named_stop(self) -> None:
        model = ScriptedModel([])
        runner = AgentRunner.for_workspace(
            settings=self._settings(thinking_mode="enabled", context_char_budget=2_000),
            model_client=model,
            workspace=self.workspace,
        )
        outcome = runner.run("task " * 1_000)
        self.assertEqual(outcome.state, RunState.STOP_CONTEXT_BUDGET)
        self.assertEqual(model.requests, [])

    def test_trace_does_not_persist_raw_reasoning_content(self) -> None:
        sensitive_reasoning = "private-chain-of-thought-should-not-be-persisted"
        model = ScriptedModel([ModelResponse("done", reasoning_content=sensitive_reasoning)])
        trace = TraceWriter(self.workspace / "traces")
        runner = AgentRunner.for_workspace(
            settings=self._settings(thinking_mode="enabled"),
            model_client=model,
            workspace=self.workspace,
            trace=trace,
        )
        outcome = runner.run("Return a final answer")
        self.assertEqual(outcome.state, RunState.FINAL)
        trace_output = trace.path.read_text(encoding="utf-8")
        self.assertNotIn(sensitive_reasoning, trace_output)
        self.assertIn("sha256", trace_output)
