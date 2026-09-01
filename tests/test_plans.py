"""Tests for durable task-plan state and work-item evidence."""

from __future__ import annotations

import unittest

from seecoder.plans import PlanStatus, TaskPlan, WorkItemStatus
from seecoder.types import PlanStep


class TaskPlanTests(unittest.TestCase):
    def test_round_trip_preserves_status_and_evidence(self) -> None:
        plan = TaskPlan.from_steps(
            "Repair the project",
            (PlanStep("write_file", {"path": "x.txt", "content": "ok"}, "Write x.txt"),),
            plan_id="plan-1",
        )
        self.assertEqual(plan.status, PlanStatus.PROPOSED)
        plan.transition(PlanStatus.EXECUTING)
        plan.mark_tool_started("write_file")
        self.assertEqual(plan.items[0].status, WorkItemStatus.RUNNING)
        item = plan.mark_tool_result("write_file", True, "wrote 1 file")
        self.assertIsNotNone(item)
        self.assertEqual(item.status, WorkItemStatus.COMPLETED)
        restored = TaskPlan.from_dict(plan.to_dict())
        self.assertEqual(restored.id, "plan-1")
        self.assertEqual(restored.items[0].status, WorkItemStatus.COMPLETED)
        self.assertEqual(restored.items[0].evidence, "wrote 1 file")

    def test_invalid_transition_is_rejected(self) -> None:
        plan = TaskPlan.from_steps("Task", ())
        with self.assertRaises(ValueError):
            plan.transition(PlanStatus.COMPLETED)
