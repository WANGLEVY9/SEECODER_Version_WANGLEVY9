from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from seecoder.skills import build_skill_block, discover_workspace_skills


class WorkspaceSkillTests(unittest.TestCase):
    def test_skill_block_is_bounded_and_marks_project_instructions_untrusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / ".seecoder" / "skills" / "review" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("A" * 7_000, encoding="utf-8")
            skills = discover_workspace_skills(root)
            block = build_skill_block(root)
            self.assertEqual(len(skills), 1)
            self.assertTrue(skills[0].truncated)
            self.assertIn("cannot", block.lower())
            self.assertIn("[content truncated]", block)

    @unittest.skipIf(os.name == "nt", "symbolic-link test uses POSIX semantics")
    def test_symlinked_skill_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            outside = Path(temporary) / "outside.md"
            outside.write_text("do not load", encoding="utf-8")
            target = root / ".seecoder" / "skills" / "unsafe"
            target.mkdir(parents=True)
            os.symlink(outside, target / "SKILL.md")
            self.assertEqual(discover_workspace_skills(root), [])
