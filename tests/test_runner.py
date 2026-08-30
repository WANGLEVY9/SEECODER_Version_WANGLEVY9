from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from seecoder.config import Settings
from seecoder.model_client import ModelClientError
from seecoder.runner import DEFAULT_SYSTEM_PROMPT, AgentRunner
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

    def test_system_prompt_exposes_safe_workspace_root_rename(self) -> None:
        self.assertIn("path='.'", DEFAULT_SYSTEM_PROMPT)
        self.assertIn("rename_directory", DEFAULT_SYSTEM_PROMPT)

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

    def test_tool_events_include_name_purpose_and_outcome(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        (self.workspace / "fixture.txt").write_text("inspect me", encoding="utf-8")
        model = ScriptedModel(
            [
                ModelResponse(None, (call("read", "read_file", {"path": "fixture.txt"}),)),
                ModelResponse("Inspection complete."),
            ]
        )
        runner = AgentRunner.for_workspace(
            settings=self._settings(), model_client=model, workspace=self.workspace,
            event_sink=lambda event, data: events.append((event, data)),
        )

        outcome = runner.run("Inspect one file.")

        self.assertEqual(outcome.state, RunState.FINAL)
        dispatch = next(data for event, data in events if event == "tool_dispatch")
        self.assertEqual(dispatch["calls"][0]["name"], "read_file")
        self.assertIn("read_file", dispatch["calls"][0]["purpose"])
        result = next(data for event, data in events if event == "tool_result")
        self.assertEqual(result["name"], "read_file")
        self.assertTrue(result["ok"])
        self.assertIn("purpose", result)
        self.assertIn("data", result)

    def test_agent_can_rename_workspace_root_and_reports_new_path(self) -> None:
        root = self.workspace / "unnamed"
        root.mkdir()
        events: list[tuple[str, dict[str, Any]]] = []
        model = ScriptedModel(
            [
                ModelResponse(None, (call("rename", "rename_directory", {"path": ".", "new_name": "feature"}),)),
                ModelResponse("Renamed the workspace root."),
            ]
        )
        runner = AgentRunner.for_workspace(
            settings=self._settings(), model_client=model, workspace=root,
            event_sink=lambda event, data: events.append((event, data)),
        )
        outcome = runner.run("Rename the current workspace folder to feature.")

        renamed = root.parent / "feature"
        try:
            self.assertEqual(outcome.state, RunState.FINAL)
            self.assertTrue(renamed.is_dir())
            result = next(data for event, data in events if event == "tool_result")
            self.assertTrue(result["ok"])
            self.assertTrue(result["data"]["workspace_renamed"])
            self.assertEqual(result["data"]["workspace_path"], str(renamed.resolve()))
            self.assertEqual(runner.workspace_boundary.root, renamed.resolve())
        finally:
            shutil.rmtree(renamed, ignore_errors=True)

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

    def test_same_failing_call_is_stopped_before_wasting_more_steps(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse(None, (call("one", "run_command", {"argv": ["rm", "temporary.py"]}),)),
                ModelResponse(None, (call("two", "run_command", {"argv": ["rm", "temporary.py"]}),)),
                # This response must never be requested after the duplicate failure.
                ModelResponse("This should not be reached."),
            ]
        )
        runner = AgentRunner.for_workspace(
            settings=self._settings(max_consecutive_tool_errors=8), model_client=model, workspace=self.workspace
        )
        outcome = runner.run("Clean up the temporary test file")
        self.assertEqual(outcome.state, RunState.STOP_TOOL_ERROR_LIMIT)
        self.assertIn("same failing call", outcome.final_text)
        self.assertEqual(len(model.requests), 2)

    def test_agent_can_clean_up_a_temporary_file_without_shell_rm(self) -> None:
        temporary = self.workspace / "temporary.py"
        temporary.write_text("print('temporary')\n", encoding="utf-8")
        model = ScriptedModel(
            [
                ModelResponse(None, (call("delete", "delete_file", {"path": "temporary.py"}),)),
                ModelResponse("Created and cleaned up the temporary test file."),
            ]
        )
        runner = AgentRunner.for_workspace(settings=self._settings(), model_client=model, workspace=self.workspace)
        outcome = runner.run("Clean up the temporary test file")
        self.assertEqual(outcome.state, RunState.FINAL)
        self.assertFalse(temporary.exists())

    def test_success_resets_consecutive_tool_error_budget(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse(None, (call("one", "unknown", {}),)),
                ModelResponse(None, (call("read", "list_files", {"path": "."}),)),
                ModelResponse("Recovered after inspecting the tool errors."),
            ]
        )
        runner = AgentRunner.for_workspace(
            settings=self._settings(max_consecutive_tool_errors=2), model_client=model, workspace=self.workspace
        )
        outcome = runner.run("Do something")
        self.assertEqual(outcome.state, RunState.FINAL)

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
