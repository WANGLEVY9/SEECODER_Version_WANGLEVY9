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
    CopyFileTool,
    CreateDirectoryTool,
    DeleteFileTool,
    GitDiffTool,
    GitLogTool,
    GitStatusTool,
    GitShowTool,
    ListFilesTool,
    ListSkillsTool,
    MoveFileTool,
    FindFilesTool,
    ProjectOverviewTool,
    ReadFileTool,
    RenameDirectoryTool,
    RunCommandTool,
    SearchCodeTool,
    SearchFilesTool,
    ToolRegistry,
    WebSearchTool,
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
        self.assertEqual(created.data["added_lines"], 1)
        self.assertEqual(created.data["deleted_lines"], 0)

        outside = ToolRegistry.create([tool]).dispatch(
            ToolCall(id="escape", name="write_file", arguments=json.dumps({"path": "../outside.txt", "content": "no"}))
        )
        self.assertFalse(outside.ok)
        self.assertEqual(outside.error.kind, "ValidationError")

    def test_delete_file_removes_one_workspace_file_and_protects_metadata(self) -> None:
        temporary = self.root / "temporary-test.py"
        temporary.write_text("print('ok')\n", encoding="utf-8")
        result = ToolRegistry.create([DeleteFileTool(self.boundary)]).dispatch(
            ToolCall(id="delete", name="delete_file", arguments=json.dumps({"path": "temporary-test.py"}))
        )
        self.assertTrue(result.ok)
        self.assertFalse(temporary.exists())
        self.assertEqual(result.data["deleted_lines"], 1)
        protected = self.root / ".git"
        protected.mkdir()
        (protected / "config").write_text("private", encoding="utf-8")
        blocked = DeleteFileTool(self.boundary).execute({"path": ".git/config"})
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error.kind, "ProtectedPath")

    def test_create_copy_and_move_file_tools_are_workspace_confined(self) -> None:
        create = CreateDirectoryTool(self.boundary).execute({"path": "generated/nested"})
        self.assertTrue(create.ok)
        source = self.root / "src" / "sample.txt"
        copied = CopyFileTool(self.boundary).execute({"source": "src/sample.txt", "destination": "generated/nested/copy.txt"})
        self.assertTrue(copied.ok)
        self.assertEqual((self.root / "generated/nested/copy.txt").read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
        self.assertEqual(copied.data["added_lines"], 3)
        moved = MoveFileTool(self.boundary).execute({"source": "generated/nested/copy.txt", "destination": "generated/moved.txt"})
        self.assertTrue(moved.ok)
        self.assertFalse((self.root / "generated/nested/copy.txt").exists())
        self.assertTrue((self.root / "generated/moved.txt").is_file())
        overwrite = CopyFileTool(self.boundary).execute({"source": "src/sample.txt", "destination": "generated/moved.txt"})
        self.assertFalse(overwrite.ok)
        self.assertEqual(overwrite.error.kind, "DestinationExists")

    def test_rename_directory_moves_code_folder_inside_workspace(self) -> None:
        source = self.root / "src" / "legacy"
        source.mkdir()
        (source / "module.py").write_text("value = 1\n", encoding="utf-8")
        result = RenameDirectoryTool(self.boundary).execute({"path": "src/legacy", "new_name": "core"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["old_path"], "src/legacy")
        self.assertEqual(result.data["new_path"], "src/core")
        self.assertTrue((self.root / "src" / "core" / "module.py").is_file())

    def test_rename_directory_moves_workspace_root_and_updates_boundary(self) -> None:
        old_root = self.boundary.root
        root_result = RenameDirectoryTool(self.boundary).execute({"path": ".", "new_name": "renamed"})
        self.assertTrue(root_result.ok)
        self.assertEqual(root_result.data["old_path"], str(old_root))
        renamed = old_root.parent / "renamed"
        self.assertEqual(root_result.data["new_path"], str(renamed))
        self.assertEqual(self.boundary.root, renamed.resolve())
        self.assertTrue((renamed / "src" / "sample.txt").is_file())

    def test_rename_directory_refuses_protected_folders(self) -> None:
        protected = self.root / ".git"
        protected.mkdir()
        protected_result = RenameDirectoryTool(self.boundary).execute({"path": ".git", "new_name": "old-git"})
        self.assertFalse(protected_result.ok)
        self.assertEqual(protected_result.error.kind, "ProtectedPath")

    def test_find_files_and_project_overview_are_bounded_read_only_tools(self) -> None:
        (self.root / "src" / "sample.py").write_text("print('ok')\n", encoding="utf-8")
        found = FindFilesTool(self.boundary).execute({"pattern": "**/*.py"})
        overview = ProjectOverviewTool(self.boundary).execute({})
        self.assertTrue(found.ok)
        self.assertIn("src/sample.py", found.data["matches"])
        self.assertTrue(overview.ok)
        self.assertEqual(overview.data["files_by_extension"][".py"], 1)

    def test_git_show_returns_commit_summary_without_mutation(self) -> None:
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "--quiet", "-m", "fixture"],
            cwd=self.root,
            check=True,
        )
        result = GitShowTool(self.boundary).execute({"revision": "HEAD"})
        self.assertTrue(result.ok)
        self.assertIn("fixture", result.data["summary"])

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
        self.assertIn("Available tools: read_file", unknown.error.message)

    def test_dispatch_enforces_declared_schema_before_tool_execution(self) -> None:
        registry = ToolRegistry.create([ReadFileTool(self.boundary)])
        missing = registry.dispatch(ToolCall(id="1", name="read_file", arguments="{}"))
        unknown = registry.dispatch(ToolCall(id="2", name="read_file", arguments=json.dumps({"path": "src/sample.txt", "extra": 1})))
        wrong_type = registry.dispatch(ToolCall(id="3", name="read_file", arguments=json.dumps({"path": 1})))
        self.assertEqual(missing.error.kind, "InvalidArguments")
        self.assertIn("path is required", missing.error.message)
        self.assertIn("unknown field", unknown.error.message)
        self.assertIn("must be string", wrong_type.error.message)

    def test_dispatch_enforces_array_bounds(self) -> None:
        tool = RunCommandTool(self.boundary, execution_mode="restricted")
        registry = ToolRegistry.create([tool])
        empty = registry.dispatch(ToolCall(id="empty", name="run_command", arguments=json.dumps({"argv": []})))
        oversized = registry.dispatch(ToolCall(id="oversized", name="run_command", arguments=json.dumps({"argv": ["git"] * 33})))
        self.assertIn("at least 1", empty.error.message)
        self.assertIn("at most 32", oversized.error.message)

    def test_restricted_mode_supports_common_project_validation_commands(self) -> None:
        tool = RunCommandTool(self.boundary, execution_mode="restricted")
        self.assertEqual(tool._restricted_argv({"argv": ["npm", "test"]}), ["npm", "test"])
        self.assertEqual(tool._restricted_argv({"argv": ["cargo", "check"]}), ["cargo", "check"])
        self.assertEqual(tool._restricted_argv({"argv": ["swift", "build"]}), ["swift", "build"])
        self.assertEqual(tool._restricted_argv({"argv": ["java", "-version"]}), ["java", "-version"])
        self.assertEqual(tool._restricted_argv({"argv": ["mvn", "-version"]}), ["mvn", "-version"])
        self.assertEqual(tool._restricted_argv({"argv": ["node", "-v"]}), ["node", "-v"])
        self.assertEqual(tool._restricted_argv({"argv": ["npm", "-v"]}), ["npm", "-v"])
        self.assertEqual(tool._restricted_argv({"argv": ["mvn", "test"]}), ["mvn", "test"])
        blocked = tool._restricted_argv({"argv": ["npm", "install"]})
        self.assertFalse(blocked.ok)
        self.assertIn("limited", blocked.error.message)

    def test_missing_restricted_executable_is_environment_evidence(self) -> None:
        tool = RunCommandTool(self.boundary, execution_mode="restricted")
        result = tool.execute({"argv": ["mvn", "-version"]})
        # The host running the tests may or may not have Maven installed. If
        # it is absent, this must be a recoverable command observation rather
        # than a ToolRegistry-level FilesystemError that consumes the runner's
        # repeated-error budget.
        self.assertTrue(result.ok)
        self.assertIn("exit_code", result.data)

    def test_mutation_tools_cannot_write_protected_directories(self) -> None:
        protected = self.root / ".git"
        protected.mkdir()
        result = WriteFileTool(self.boundary).execute({"path": ".git/config", "content": "unsafe"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, "ProtectedPath")

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

    def test_web_search_parses_results_with_mocked_fetcher(self) -> None:
        html_page = (
            '<a class="result__a" href="https://example.com/page">Example <b>Title</b></a>'
            '<a class="result__snippet">A short snippet.</a>'
        )
        tool = WebSearchTool(fetcher=lambda _query: html_page)
        result = tool.execute({"query": "example"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["results"][0]["url"], "https://example.com/page")
        self.assertIn("Example Title", result.data["results"][0]["title"])
        self.assertEqual(result.data["results"][0]["snippet"], "A short snippet.")

    def test_web_search_degrades_gracefully_when_fetch_fails(self) -> None:
        def broken(_query: str) -> str:
            raise OSError("network down")

        tool = WebSearchTool(fetcher=broken)
        result = tool.execute({"query": "anything"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error.kind, "WebSearchUnavailable")

    def test_search_code_finds_symbol_definitions(self) -> None:
        (self.root / "src" / "app.py").write_text(
            "class Greeter:\n    def greet(self, name):\n        return \"hi\"\n\ndef helper():\n    return 1\n",
            encoding="utf-8",
        )
        tool = SearchCodeTool(self.boundary)
        result = tool.execute({"query": "greet"})
        self.assertTrue(result.ok)
        symbols = [match["symbol"] for match in result.data["matches"]]
        self.assertIn("greet", symbols)
        self.assertIn("Greeter", symbols)
        helper = tool.execute({"query": "helper"})
        self.assertTrue(helper.ok)
        self.assertEqual(helper.data["matches"][0]["symbol"], "helper")
        self.assertEqual(helper.data["matches"][0]["kind"], "def")

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
        self.assertEqual(result.data["added_lines"], 1)
        self.assertEqual(result.data["deleted_lines"], 1)

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

    def test_git_diff_includes_staged_and_untracked_files(self) -> None:
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        target = self.root / "src" / "sample.txt"
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "--quiet", "-m", "fixture"],
            cwd=self.root,
            check=True,
        )
        target.write_text("staged-change\n", encoding="utf-8")
        subprocess.run(["git", "add", str(target)], cwd=self.root, check=True)
        untracked = self.root / "new.py"
        untracked.write_text("print('new')\n", encoding="utf-8")

        result = GitDiffTool(self.boundary).execute({})
        self.assertTrue(result.ok)
        self.assertTrue(result.data["staged"])
        self.assertTrue(result.data["untracked"])
        self.assertIn("staged-change", result.data["diff"])
        self.assertIn("print('new')", result.data["diff"])

    def test_git_status_and_log_are_bounded_and_read_only(self) -> None:
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        subprocess.run(["git", "add", "src/sample.txt"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "--quiet", "-m", "fixture"],
            cwd=self.root,
            check=True,
        )
        (self.root / "new.txt").write_text("uncommitted\n", encoding="utf-8")
        status = GitStatusTool(self.boundary).execute({"max_entries": 1})
        history = GitLogTool(self.boundary).execute({"max_commits": 1})
        self.assertTrue(status.ok)
        self.assertIn("new.txt", "\n".join(status.data["status"]))
        self.assertTrue(history.ok)
        self.assertEqual(history.data["commits"][0]["subject"], "fixture")

    def test_list_skills_discovers_only_bounded_local_packages(self) -> None:
        skill = self.root / ".seecoder" / "skills" / "python-review" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("Prefer focused Python tests.", encoding="utf-8")
        result = ListSkillsTool(self.boundary).execute({})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["skills"][0]["name"], "python-review")
        self.assertEqual(result.data["skills"][0]["path"], ".seecoder/skills/python-review/SKILL.md")
