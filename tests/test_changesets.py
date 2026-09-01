from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from seecoder.changesets import ChangeSetJournal
from seecoder.types import ToolCall, ToolResult


def call(name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall("call-1", name, json.dumps(arguments))


class ChangeSetJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        self.storage = Path(self.temporary.name) / "journal"
        self.journal = ChangeSetJournal(self.workspace, self.storage)
        self.journal.start("run-1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_persists_baseline_and_rolls_back_a_write(self) -> None:
        target = self.workspace / "example.txt"
        target.write_text("before\n", encoding="utf-8")
        mutation = call("write_file", {"path": "example.txt", "content": "after\n"})
        before = self.journal.capture_before(mutation)
        target.write_text("after\n", encoding="utf-8")

        change = self.journal.record(mutation, ToolResult.success({"path": "example.txt"}), before)

        self.assertIsNotNone(change)
        changeset_id = str(change["changeset_id"])
        self.assertTrue((self.storage / f"{changeset_id}.json").is_file())
        result = self.journal.rollback(changeset_id)
        self.assertTrue(result["ok"])
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")

    def test_refuses_rollback_after_a_later_edit(self) -> None:
        target = self.workspace / "example.txt"
        target.write_text("before\n", encoding="utf-8")
        mutation = call("write_file", {"path": "example.txt", "content": "after\n"})
        before = self.journal.capture_before(mutation)
        target.write_text("after\n", encoding="utf-8")
        change = self.journal.record(mutation, ToolResult.success({"path": "example.txt"}), before)
        target.write_text("user edit\n", encoding="utf-8")

        result = self.journal.rollback(str(change["changeset_id"]))

        self.assertFalse(result["ok"])
        self.assertEqual(result["conflicts"], ["example.txt"])
        self.assertEqual(target.read_text(encoding="utf-8"), "user edit\n")

    def test_records_creation_and_deletion_with_a_missing_baseline(self) -> None:
        mutation = call("write_file", {"path": "new.txt", "content": "new\n"})
        before = self.journal.capture_before(mutation)
        (self.workspace / "new.txt").write_text("new\n", encoding="utf-8")
        change = self.journal.record(mutation, ToolResult.success({"path": "new.txt"}), before)

        self.assertEqual(change["files"], ["new.txt"])
        result = self.journal.rollback(str(change["changeset_id"]))
        self.assertTrue(result["ok"])
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_rejects_untrusted_changeset_identifiers(self) -> None:
        result = self.journal.rollback("../../outside")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "ChangeSet identifier is invalid.")

    def test_cli_rollback_uses_the_same_hash_guard(self) -> None:
        target = self.workspace / "example.txt"
        target.write_text("before\n", encoding="utf-8")
        mutation = call("write_file", {"path": "example.txt", "content": "after\n"})
        before = self.journal.capture_before(mutation)
        target.write_text("after\n", encoding="utf-8")
        change = self.journal.record(mutation, ToolResult.success({"path": "example.txt"}), before)

        from contextlib import redirect_stdout
        from io import StringIO
        from seecoder.cli import main
        output = StringIO()
        with redirect_stdout(output):
            code = main(["rollback-changeset", "--workspace", str(self.workspace), "--journal-dir", str(self.storage), "--changeset-id", str(change["changeset_id"])])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["ok"], True)
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")


if __name__ == "__main__":
    unittest.main()
