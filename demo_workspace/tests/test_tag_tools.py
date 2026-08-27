from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from tag_tools import normalize_tag  # noqa: E402


class NormalizeTagTests(unittest.TestCase):
    def test_normalizes_case_and_outer_whitespace(self) -> None:
        self.assertEqual(normalize_tag("  Feature-Flag  "), "feature-flag")


if __name__ == "__main__":
    unittest.main()
