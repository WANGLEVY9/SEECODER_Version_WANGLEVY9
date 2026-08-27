from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from seecoder.trace import TraceWriter


class TraceTests(unittest.TestCase):
    def test_known_secret_and_sensitive_key_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            writer = TraceWriter(Path(temporary), secrets=("real-secret",))
            writer.record("example", {"api_key": "other-secret", "note": "contains real-secret"})
            output = writer.path.read_text(encoding="utf-8")
        self.assertNotIn("real-secret", output)
        self.assertNotIn("other-secret", output)
        self.assertIn("[REDACTED]", output)
