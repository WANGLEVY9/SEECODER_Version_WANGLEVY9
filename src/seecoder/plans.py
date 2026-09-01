"""Persistent task plans and work-item state transitions."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from seecoder.types import PlanStep


class PlanStatus(StrEnum):
    PROPOSED = "proposed"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class WorkItem:
    id: str
    description: str
    tool: str
    arguments: dict[str, Any]
    status: WorkItemStatus = WorkItemStatus.PENDING
    evidence: str = ""


@dataclass(slots=True)
class TaskPlan:
    id: str
    task: str
    status: PlanStatus
    items: list[WorkItem] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_steps(cls, task: str, steps: Iterable[PlanStep], *, plan_id: str | None = None) -> TaskPlan:
        now = datetime.now(UTC).isoformat()
        plan = cls(plan_id or str(uuid.uuid4()), task, PlanStatus.PROPOSED, created_at=now, updated_at=now)
        for step in steps:
            plan.add_step(step)
        return plan

    def add_step(self, step: PlanStep) -> WorkItem:
        item = WorkItem(str(uuid.uuid4()), step.description, step.tool, dict(step.arguments))
        self.items.append(item)
        self.updated_at = datetime.now(UTC).isoformat()
        return item

    def transition(self, status: PlanStatus, *, evidence: str = "") -> None:
        allowed = {
            PlanStatus.PROPOSED: {PlanStatus.EXECUTING, PlanStatus.CANCELLED},
            PlanStatus.EXECUTING: {PlanStatus.VERIFYING, PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED},
            PlanStatus.VERIFYING: {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED},
            PlanStatus.COMPLETED: set(), PlanStatus.FAILED: {PlanStatus.EXECUTING}, PlanStatus.CANCELLED: set(),
        }
        if status != self.status and status not in allowed[self.status]:
            raise ValueError(f"Invalid task-plan transition: {self.status} -> {status}")
        self.status = status
        if evidence:
            for item in self.items:
                if item.status in {WorkItemStatus.PENDING, WorkItemStatus.RUNNING}:
                    item.evidence = evidence
        self.updated_at = datetime.now(UTC).isoformat()

    def mark_tool_result(self, tool: str, ok: bool, evidence: str) -> WorkItem | None:
        item = next(
            (item for item in self.items if item.tool == tool and item.status in {
                WorkItemStatus.PENDING, WorkItemStatus.RUNNING
            }),
            None,
        )
        if item is None:
            return None
        item.status = WorkItemStatus.COMPLETED if ok else WorkItemStatus.FAILED
        item.evidence = evidence
        self.updated_at = datetime.now(UTC).isoformat()
        return item

    def mark_tool_started(self, tool: str) -> WorkItem | None:
        item = next(
            (item for item in self.items if item.tool == tool and item.status == WorkItemStatus.PENDING),
            None,
        )
        if item is None:
            return None
        item.status = WorkItemStatus.RUNNING
        self.updated_at = datetime.now(UTC).isoformat()
        return item

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        for item in value["items"]:
            item["status"] = item["status"].value if isinstance(item["status"], WorkItemStatus) else item["status"]
        return value

    @classmethod
    def from_dict(cls, raw: object) -> TaskPlan:
        if not isinstance(raw, dict):
            raise ValueError("task_plan must be an object")
        required = {"id", "task", "status", "items", "created_at", "updated_at"}
        if set(raw) != required or not all(isinstance(raw[key], str) for key in ("id", "task", "status", "created_at", "updated_at")):
            raise ValueError("task_plan fields are invalid")
        status = PlanStatus(raw["status"])
        if not isinstance(raw["items"], list):
            raise ValueError("task_plan.items must be a list")
        items: list[WorkItem] = []
        for item in raw["items"]:
            if not isinstance(item, dict) or set(item) != {"id", "description", "tool", "arguments", "status", "evidence"}:
                raise ValueError("task_plan work item fields are invalid")
            if not all(isinstance(item[key], str) for key in ("id", "description", "tool", "status", "evidence")) or not isinstance(item["arguments"], dict):
                raise ValueError("task_plan work item values are invalid")
            items.append(WorkItem(item["id"], item["description"], item["tool"], dict(item["arguments"]), WorkItemStatus(item["status"]), item["evidence"]))
        return cls(raw["id"], raw["task"], status, items, raw["created_at"], raw["updated_at"])
