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


class StreamingModel:
    """A model that only supports streaming; each call yields the next turn's events."""

    def __init__(self, turns: list[list[Any]]) -> None:
        self.turns = turns
        self.requests: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage], tools: list[dict[str, Any]]) -> ModelResponse:
        raise AssertionError("non-streaming complete should not be called when streaming is wired")

    def complete_stream(self, messages: list[ChatMessage], tools: list[dict[str, Any]]):
        self.requests.append(messages)
        for event in self.turns.pop(0):
            yield event


def call(identifier: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id=identifier, name=name, arguments=json.dumps(arguments))


class ApprovalPolicyTests(unittest.TestCase):
    def test_read_only_tools_run_in_every_mode(self) -> None:
        for mode in (Mode.AUTO, Mode.PLAN, Mode.ASK):
            for tool_name in ("list_files", "read_file", "search_code", "git_status", "git_log", "list_skills"):
                self.assertTrue(is_read_only(tool_name))
                self.assertEqual(Policy(mode).decide(tool_name), ApprovalDecision.ALLOW)

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

    def test_streaming_forwards_deltas_and_consumes_assembled_response(self) -> None:
        from seecoder.types import StreamEvent

        model = StreamingModel(
            [
                [
                    StreamEvent(kind="content_delta", text="Hello "),
                    StreamEvent(kind="content_delta", text="world"),
                    StreamEvent(kind="done", response=ModelResponse("Hello world", usage=Usage(7, 3, 10))),
                ]
            ]
        )
        deltas: list[StreamEvent] = []
        runner = AgentRunner.for_workspace(
            settings=self._settings(), model_client=model, workspace=self.workspace,
            stream_sink=deltas.append,
        )
        outcome = runner.run("Say hi")
        self.assertEqual(outcome.state, RunState.FINAL)
        self.assertEqual(outcome.final_text, "Hello world")
        self.assertEqual([event.kind for event in deltas], ["content_delta", "content_delta", "done"])
        self.assertEqual(outcome.usage.total_tokens, 10)

    def test_streaming_tool_call_still_executes_local_tool(self) -> None:
        from seecoder.types import StreamEvent

        (self.workspace / "a.txt").write_text("hi", encoding="utf-8")
        model = StreamingModel(
            [
                [
                    StreamEvent(kind="tool_call_delta", index=0, call_id="c1", name="read_file"),
                    StreamEvent(kind="done", response=ModelResponse(
                        None, (call("c1", "read_file", {"path": "a.txt"}),),
                    )),
                ],
                [StreamEvent(kind="done", response=ModelResponse("Read a.txt."))],
            ]
        )
        runner = AgentRunner.for_workspace(
            settings=self._settings(), model_client=model, workspace=self.workspace,
            stream_sink=lambda _event: None,
        )
        outcome = runner.run("Read a.txt")
        self.assertEqual(outcome.state, RunState.FINAL)
        self.assertEqual(len(model.requests), 2)

    def test_parallel_read_only_tool_calls_run_and_feed_back(self) -> None:
        (self.workspace / "a.txt").write_text("A", encoding="utf-8")
        (self.workspace / "b.txt").write_text("B", encoding="utf-8")
        model = ScriptedModel(
            [
                ModelResponse(None, (call("r1", "read_file", {"path": "a.txt"}),
                                     call("r2", "read_file", {"path": "b.txt"}),
                                     call("r3", "list_files", {"path": "."}))),
                ModelResponse("Inspected."),
            ]
        )
        runner = AgentRunner.for_workspace(settings=self._settings(), model_client=model, workspace=self.workspace)
        outcome = runner.run("Inspect")
        self.assertEqual(outcome.state, RunState.FINAL)
        self.assertEqual(len(model.requests), 2)
        # All three read-only tool results were fed back to the model in order.
        tool_results = [message for message in model.requests[1] if message.role == "tool"]
        self.assertEqual(len(tool_results), 3)
        self.assertEqual(tool_results[0].tool_call_id, "r1")
        self.assertEqual(tool_results[2].tool_call_id, "r3")

    def test_spawn_agent_runs_a_bounded_subagent(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse(None, (call("s1", "spawn_agent", {"name": "reviewer", "task": "review the code", "max_steps": 3}),)),
                ModelResponse("Reviewed: looks OK."),  # consumed by the sub-agent
                ModelResponse("Done with review."),    # consumed by the outer agent
            ]
        )
        runner = AgentRunner.for_workspace(settings=self._settings(), model_client=model, workspace=self.workspace)
        outcome = runner.run("Run a review")
        self.assertEqual(outcome.state, RunState.FINAL)
        self.assertIn("Done with review", outcome.final_text)
        # Two outer turns plus one sub-agent turn.
        self.assertEqual(len(model.requests), 3)

    def test_context_compaction_collapses_older_history(self) -> None:
        runner = AgentRunner.for_workspace(
            settings=self._settings(context_char_budget=2_000),
            model_client=ScriptedModel([ModelResponse("done")]),
            workspace=self.workspace,
            compactor=lambda prefix: "compact summary note",
        )
        messages = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="task")]
        for index in range(8):
            messages.append(ChatMessage(role="assistant", content="step", tool_calls=(call(f"c{index}", "list_files", {"path": "."}),)))
            messages.append(ChatMessage(role="tool", tool_call_id=f"c{index}", content="x" * 200))
        compacted = runner._maybe_compact(messages)
        self.assertTrue(compacted)
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[1].role, "user")
        self.assertIn("<compacted_context>", messages[2].content)
        # The compacted history drops the older turns while keeping system+task+recent tail.
        self.assertLessEqual(len(messages), 7)

    def test_compaction_is_skipped_in_thinking_mode(self) -> None:
        runner = AgentRunner.for_workspace(
            settings=self._settings(context_char_budget=2_000, thinking_mode="enabled"),
            model_client=ScriptedModel([ModelResponse("done")]),
            workspace=self.workspace,
            compactor=lambda prefix: "compact summary note",
        )
        messages = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="t")]
        for index in range(6):
            messages.append(ChatMessage(role="assistant", content=None, tool_calls=(call(f"c{index}", "list_files", {}),), reasoning_content="reasoning" * 100))
            messages.append(ChatMessage(role="tool", tool_call_id=f"c{index}", content="x" * 200))
        compacted = runner._maybe_compact(messages)
        self.assertFalse(compacted)
        self.assertEqual(len(messages), 14)

    def test_project_memory_is_injected_into_system_prompt(self) -> None:
        (self.workspace / "SEECODER.md").write_text("Remember to strip whitespace in normalize_tag.", encoding="utf-8")
        model = ScriptedModel([ModelResponse("done")])
        runner = AgentRunner.for_workspace(settings=self._settings(), model_client=model, workspace=self.workspace)
        runner.run("Do it")
        system = model.requests[0][0]
        self.assertEqual(system.role, "system")
        self.assertIn("<project_memory>", system.content)
        self.assertIn("Remember to strip whitespace", system.content)

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
