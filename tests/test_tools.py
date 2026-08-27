from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import subprocess
from pathlib import Path

from seecoder.tools import (
    ApplyPatchTool,
    GitDiffTool,
    ListFilesTool,
    ReadFileTool,
    RunCommandTool,
    SearchFilesTool,
    ToolRegistry,
    WorkspaceBoundary,
    WriteFileTool,
)
from seecoder.types import ToolCall


class LocalToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "sample.txt").write_text("first\nsecond\nthird\n", encoding="utf-8")
        self.boundary = WorkspaceBoundary(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_list_and_read_file_with_line_numbers(self) -> None:
        listed = ListFilesTool(self.boundary).execute({"path": ".", "max_depth": 2})
        self.assertTrue(listed.ok)
        self.assertIn("src/sample.txt", [item["path"] for item in listed.data["entries"]])

        read = ReadFileTool(self.boundary).execute({"path": "src/sample.txt", "start_line": 2, "end_line": 3})
        self.assertTrue(read.ok)
        self.assertEqual(read.data["content"], "2: second\n3: third")

    def test_write_is_atomic_and_rejects_workspace_escape(self) -> None:
        tool = WriteFileTool(self.boundary)
        created = tool.execute({"path": "generated/output.py", "content": "print('ok')\n"})
        self.assertTrue(created.ok)
        self.assertEqual((self.root / "generated" / "output.py").read_text(encoding="utf-8"), "print('ok')\n")

        outside = ToolRegistry.create([tool]).dispatch(
            ToolCall(id="escape", name="write_file", arguments=json.dumps({"path": "../outside.txt", "content": "no"}))
        )
        self.assertFalse(outside.ok)
        self.assertEqual(outside.error.kind, "ValidationError")

    @unittest.skipIf(os.name == "nt", "symbolic-link test uses POSIX semantics")
    def test_symlink_escape_is_rejected(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        os.symlink(outside, self.root / "linked-outside")

        result = ToolRegistry.create([ReadFileTool(self.boundary)]).dispatch(
            ToolCall(id="link", name="read_file", arguments=json.dumps({"path": "linked-outside/secret.txt"}))
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, "ValidationError")

    def test_dispatch_rejects_invalid_json_and_unknown_tools(self) -> None:
        registry = ToolRegistry.create([ReadFileTool(self.boundary)])
        malformed = registry.dispatch(ToolCall(id="1", name="read_file", arguments="{"))
        unknown = registry.dispatch(ToolCall(id="2", name="not_real", arguments="{}"))
        self.assertEqual(malformed.error.kind, "InvalidArguments")
        self.assertEqual(unknown.error.kind, "UnknownTool")

    def test_command_returns_nonzero_as_observation_and_scrubs_secrets(self) -> None:
        old_value = os.environ.get("SEECODER_API_KEY")
        os.environ["SEECODER_API_KEY"] = "should-not-reach-child"
        try:
            command = f"{sys.executable} -c \"import os; print(os.getenv('SEECODER_API_KEY')); raise SystemExit(7)\""
            result = RunCommandTool(self.boundary).execute({"command": command})
        finally:
            if old_value is None:
                del os.environ["SEECODER_API_KEY"]
            else:
                os.environ["SEECODER_API_KEY"] = old_value
        self.assertTrue(result.ok)
        self.assertEqual(result.data["exit_code"], 7)
        self.assertIn("None", result.data["stdout"])

    def test_command_timeout_and_dangerous_guard(self) -> None:
        sleeping = f"{sys.executable} -c \"import time; time.sleep(2)\""
        timeout = RunCommandTool(self.boundary).execute({"command": sleeping, "timeout_s": 1})
        self.assertFalse(timeout.ok)
        self.assertEqual(timeout.error.kind, "CommandTimeout")

        blocked = RunCommandTool(self.boundary).execute({"command": "rm -rf temporary"})
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error.kind, "DangerousCommand")

    def test_dotenv_files_are_not_exposed_to_the_model(self) -> None:
        (self.root / ".env").write_text("SEECODER_API_KEY=not-for-model", encoding="utf-8")
        result = ToolRegistry.create([ReadFileTool(self.boundary)]).dispatch(
            ToolCall(id="dotenv", name="read_file", arguments=json.dumps({"path": ".env"}))
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, "SensitivePath")

    def test_large_file_is_rejected_before_unbounded_read(self) -> None:
        (self.root / "large.txt").write_text("0123456789", encoding="utf-8")
        result = ReadFileTool(self.boundary, max_bytes=5).execute({"path": "large.txt"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, "FileTooLarge")

    def test_command_output_is_drained_but_bounded(self) -> None:
        command = f"{sys.executable} -c \"print('x' * 50000)\""
        result = RunCommandTool(self.boundary, max_output_chars=128).execute({"command": command})
        self.assertTrue(result.ok)
        self.assertTrue(result.meta["truncated"])
        self.assertLessEqual(len(result.data["stdout"]), 160)

    def test_command_directly_referencing_dotenv_is_blocked(self) -> None:
        result = RunCommandTool(self.boundary).execute({"command": "cat .env"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, "DangerousCommand")

    def test_restricted_command_allows_unittest_argv_without_a_shell(self) -> None:
        (self.root / "test_safe.py").write_text(
            "import unittest\n\nclass SafeTest(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        result = RunCommandTool(self.boundary, execution_mode="restricted").execute(
            {"argv": ["python3.12", "-m", "unittest", "discover", "-s", ".", "-p", "test_safe.py"]}
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["exit_code"], 0)

    def test_restricted_command_rejects_metacharacters_inline_python_and_absolute_paths(self) -> None:
        tool = RunCommandTool(self.boundary, execution_mode="restricted")
        metacharacter = tool.execute({"argv": ["python3.12", "-m", "unittest", ">", "out.txt"]})
        inline_python = tool.execute({"argv": ["python3.12", "-c", "print('no')"]})
        absolute = tool.execute({"argv": ["python3.12", "-m", "unittest", "/tmp/test.py"]})
        self.assertEqual(metacharacter.error.kind, "RestrictedCommand")
        self.assertEqual(inline_python.error.kind, "RestrictedCommand")
        self.assertEqual(absolute.error.kind, "RestrictedCommand")

    def test_search_returns_bounded_matching_lines(self) -> None:
        (self.root / "src" / "search_target.py").write_text("first needle\nsecond needle\n", encoding="utf-8")
        result = SearchFilesTool(self.boundary).execute({"query": "needle", "max_matches": 1})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["matches"][0]["path"], "src/search_target.py")
        self.assertEqual(result.data["matches"][0]["line"], 1)
        self.assertTrue(result.meta["truncated"])

    def test_exact_context_patch_changes_one_block_atomically(self) -> None:
        path = self.root / "src" / "patch_target.py"
        path.write_text("value = 'old'\n", encoding="utf-8")
        result = ApplyPatchTool(self.boundary).execute(
            {"path": "src/patch_target.py", "old_text": "'old'", "new_text": "'new'"}
        )
        self.assertTrue(result.ok)
        self.assertEqual(path.read_text(encoding="utf-8"), "value = 'new'\n")

    def test_patch_rejects_ambiguous_or_missing_context(self) -> None:
        path = self.root / "src" / "ambiguous.py"
        path.write_text("token = 1\ntoken = 1\n", encoding="utf-8")
        result = ApplyPatchTool(self.boundary).execute(
            {"path": "src/ambiguous.py", "old_text": "token = 1", "new_text": "token = 2"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, "PatchContextMismatch")
        self.assertEqual(path.read_text(encoding="utf-8"), "token = 1\ntoken = 1\n")

    def test_git_diff_is_read_only_and_bounded(self) -> None:
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        target = self.root / "src" / "sample.txt"
        subprocess.run(["git", "add", "src/sample.txt"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "--quiet", "-m", "fixture"],
            cwd=self.root,
            check=True,
        )
        target.write_text("changed\n", encoding="utf-8")
        result = GitDiffTool(self.boundary).execute({"path": "src/sample.txt", "max_chars": 500})
        self.assertTrue(result.ok)
        self.assertIn("src/sample.txt", result.data["status"])
        self.assertIn("-first", result.data["diff"])
        self.assertIn("+changed", result.data["diff"])

    def test_git_diff_reports_non_repository(self) -> None:
        result = GitDiffTool(self.boundary).execute({})
        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, "NotGitRepository")
