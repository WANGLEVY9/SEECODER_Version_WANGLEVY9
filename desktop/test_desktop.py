"""Headless tests for the desktop UI's non-widget boundary logic.

Run with the Homebrew Python that provides Tk 9 on this macOS host:
    cd desktop && /opt/homebrew/opt/python@3.12/bin/python3.12 -m unittest test_desktop.py -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import seecoder_desktop as desktop


class DesktopBoundaryTests(unittest.TestCase):
    def test_session_store_is_local_and_round_trips_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "local" / "sessions.json"
            store = desktop.SessionStore(state_path)
            session = store.create(Path("/private/tmp/workspace"))
            session["messages"].append({"role": "user", "content": "fix a bug"})
            store.save([session])
            serialized = state_path.read_text(encoding="utf-8")
            self.assertNotIn("API_KEY", serialized)
            self.assertEqual(store.load()[0]["id"], session["id"])

    def test_backend_command_has_no_host_shell_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            command = desktop.build_backend_command("/usr/bin/uv", "inspect files", Path(temporary))
        self.assertIn("--event-json", command)
        self.assertNotIn("--host-shell", command)
        self.assertEqual(command[:4], ["/usr/bin/uv", "run", "seecoder", "run"])

    def test_event_protocol_requires_event_and_object_data(self) -> None:
        valid = json.dumps({"event": "tool_result", "data": {"name": "read_file", "ok": True}})
        self.assertEqual(desktop.parse_event_line(valid), ("tool_result", {"name": "read_file", "ok": True}))
        self.assertIsNone(desktop.parse_event_line("not json"))
        self.assertIsNone(desktop.parse_event_line(json.dumps({"event": "tool_result", "data": []})))
