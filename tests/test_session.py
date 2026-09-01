"""Tests for the interactive multi-turn Conversation and plan approval."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from seecoder.config import Settings
from seecoder.model_client import ModelClientError
from seecoder.plans import PlanStatus, WorkItemStatus
from seecoder.session import Conversation, SnapshotValidationError
from seecoder.types import Mode, ModelResponse, RunState, ToolCall


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[list[Any]] = []

    def complete(self, messages: list[Any], tools: list[dict[str, Any]]) -> ModelResponse:
        self.requests.append(messages)
        return self.responses.pop(0)


class FailOnceModel:
    """Simulate a provider timeout followed by a healthy follow-up turn."""

    def __init__(self) -> None:
        self.attempts = 0
        self.requests: list[list[Any]] = []

    def complete(self, messages: list[Any], tools: list[dict[str, Any]]) -> ModelResponse:  # noqa: ARG002
        self.attempts += 1
        self.requests.append(messages)
        if self.attempts == 1:
            raise ModelClientError("request timed out", retryable=False)
        return ModelResponse("Recovered on the follow-up turn.")


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

    def test_follow_up_after_model_failure_is_recoverable(self) -> None:
        model = FailOnceModel()
        conversation = Conversation(settings=self.settings, model_client=model, workspace=self.workspace)

        failed = conversation.start("Create a test program")
        self.assertEqual(failed.state, RunState.FAILED_MODEL)
        self.assertEqual(conversation.messages[-1].role, "assistant")

        recovered = conversation.send("Please retry that task")
        self.assertEqual(recovered.state, RunState.FINAL)
        self.assertEqual(model.attempts, 2)
        # The second provider request has a valid assistant observation between
        # the failed user turn and the new follow-up user turn.
        self.assertEqual([message.role for message in model.requests[-1]], ["system", "user", "assistant", "user"])

    def test_plan_approval_executes_previously_proposed_mutation(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse(None, (call("a", "write_file", {"path": "x.txt", "content": "hi"}),)),
                ModelResponse("Plan: create x.txt."),
                ModelResponse(None, (call("b", "write_file", {"path": "x.txt", "content": "hi"}),)),
                ModelResponse("Created x.txt."),
            ]
        )
        events: list[tuple[str, dict[str, Any]]] = []
        conversation = Conversation(
            settings=self.settings, model_client=model, workspace=self.workspace, mode=Mode.PLAN,
            event_sink=lambda event, data: events.append((event, data)),
        )
        planned = conversation.start("Create x.txt.")
        self.assertEqual(planned.state, RunState.PLAN_PROPOSED)
        self.assertEqual(planned.plan[0].tool, "write_file")
        self.assertIsNotNone(conversation.task_plan)
        self.assertEqual(conversation.task_plan.status, PlanStatus.PROPOSED)
        self.assertEqual(conversation.task_plan.items[0].status, WorkItemStatus.PENDING)
        # The file must NOT have been created during plan mode.
        self.assertFalse((self.workspace / "x.txt").exists())
        executed = conversation.approve_plan()
        self.assertEqual(executed.state, RunState.FINAL)
        self.assertEqual((self.workspace / "x.txt").read_text(encoding="utf-8"), "hi")
        self.assertEqual(conversation.task_plan.status, PlanStatus.COMPLETED)
        self.assertEqual(conversation.task_plan.items[0].status, WorkItemStatus.COMPLETED)
        plan_states = [data["status"] for event, data in events if event == "plan_state"]
        self.assertEqual(plan_states[0], "proposed")
        self.assertEqual(plan_states[-2:], ["verifying", "completed"])
        self.assertIn("executing", plan_states)

        snapshot = self.workspace / "plan.json"
        conversation.save(snapshot)
        loaded = Conversation.load(snapshot, settings=self.settings, model_client=ScriptedModel([]), workspace=self.workspace)
        self.assertEqual(loaded.task_plan.id, conversation.task_plan.id)
        self.assertEqual(loaded.task_plan.status, PlanStatus.COMPLETED)

    def test_declining_plan_persists_cancelled_state(self) -> None:
        model = ScriptedModel([
            ModelResponse(None, (call("a", "write_file", {"path": "x.txt", "content": "hi"}),)),
            ModelResponse("Plan: create x.txt."),
        ])
        conversation = Conversation(settings=self.settings, model_client=model, workspace=self.workspace, mode=Mode.PLAN)
        self.assertEqual(conversation.start("Create x.txt").state, RunState.PLAN_PROPOSED)
        conversation.cancel_plan("User declined the proposed plan.")
        self.assertEqual(conversation.task_plan.status, PlanStatus.CANCELLED)
        self.assertIn("declined", conversation.task_plan.items[0].evidence)

    def test_conversation_save_and_load_round_trips(self) -> None:
        model = ScriptedModel([ModelResponse("First answer."), ModelResponse("Second answer.")])
        conversation = Conversation(settings=self.settings, model_client=model, workspace=self.workspace)
        conversation.start("Task one")
        conversation.send("Follow up")
        path = self.workspace / "session.json"
        conversation.save(path)

        replay = ScriptedModel([])
        loaded = Conversation.load(path, settings=self.settings, model_client=replay, workspace=self.workspace)
        self.assertEqual(len(loaded.messages), len(conversation.messages))
        self.assertEqual(loaded.messages[-1].role, "assistant")
        self.assertEqual(loaded.total_usage.total_tokens, conversation.total_usage.total_tokens)
        self.assertEqual(loaded.current_mode, conversation.current_mode)
        # A resumed conversation can continue with a new turn.
        replay.responses.append(ModelResponse("Third answer."))
        outcome = loaded.send("One more")
        self.assertEqual(outcome.state, RunState.FINAL)

    def test_load_preserves_streaming_and_compaction_hooks(self) -> None:
        model = ScriptedModel([ModelResponse("First answer.")])
        conversation = Conversation(settings=self.settings, model_client=model, workspace=self.workspace)
        conversation.start("Task one")
        path = self.workspace / "session.json"
        conversation.save(path)
        stream_sink = lambda _event: None
        compactor = lambda _messages: "summary"
        loaded = Conversation.load(
            path,
            settings=self.settings,
            model_client=ScriptedModel([]),
            workspace=self.workspace,
            stream_sink=stream_sink,
            compactor=compactor,
        )
        self.assertIs(loaded.runner.stream_sink, stream_sink)
        self.assertIs(loaded.runner.compactor, compactor)

    def test_workspace_root_rename_is_persisted_for_resume(self) -> None:
        root = self.workspace / "unnamed"
        root.mkdir()
        model = ScriptedModel(
            [
                ModelResponse(None, (call("rename", "rename_directory", {"path": ".", "new_name": "renamed"}),)),
                ModelResponse("Workspace renamed."),
            ]
        )
        conversation = Conversation(settings=self.settings, model_client=model, workspace=root)
        conversation.start("Rename the current workspace to renamed.")
        renamed = root.parent / "renamed"
        try:
            self.assertEqual(conversation.workspace, renamed.resolve())
            snapshot = self.workspace / "conversation.json"
            conversation.save(snapshot)
            data = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertEqual(data["workspace"], str(renamed.resolve()))
        finally:
            shutil.rmtree(renamed, ignore_errors=True)

    def test_ask_approval_is_persisted_and_resumable(self) -> None:
        model = ScriptedModel([
            ModelResponse(None, (call("write", "write_file", {"path": "x.txt", "content": "ok"}),)),
        ])
        conversation = Conversation(settings=self.settings, model_client=model, workspace=self.workspace, mode=Mode.ASK)
        waiting = conversation.start("Write x.txt")
        self.assertEqual(waiting.state, RunState.AWAITING_APPROVAL)
        self.assertEqual(len(conversation.pending_calls), 1)
        snapshot = self.workspace / "waiting.json"
        conversation.save(snapshot)

        resumed = Conversation.load(
            snapshot, settings=self.settings,
            model_client=ScriptedModel([ModelResponse("Created x.txt.")]), workspace=self.workspace,
        )
        self.assertEqual(len(resumed.pending_calls), 1)
        finished = resumed.resolve_approval(True)
        self.assertEqual(finished.state, RunState.FINAL)
        self.assertEqual((self.workspace / "x.txt").read_text(encoding="utf-8"), "ok")

    def test_snapshot_rejects_invalid_role_and_tool_call_shape(self) -> None:
        path = self.workspace / "invalid.json"
        path.write_text(json.dumps({
            "version": 2, "mode": "auto", "workspace": str(self.workspace),
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "steps": 0,
            "pending_approval": [],
            "messages": [{"role": "hacker", "content": "x", "tool_calls": [], "tool_call_id": None, "reasoning_content": None}],
        }), encoding="utf-8")
        with self.assertRaises(SnapshotValidationError):
            Conversation.load(path, settings=self.settings, model_client=ScriptedModel([]), workspace=self.workspace)
