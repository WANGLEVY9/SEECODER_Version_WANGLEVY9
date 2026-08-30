from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import json

from seecoder.cli import _is_inside, _token_json_printer, main
from seecoder.types import StreamEvent


class CliSafetyTests(unittest.TestCase):
    def test_stream_printer_separates_content_and_reasoning_events(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            _token_json_printer(StreamEvent(kind="content_delta", text="hello"))
            _token_json_printer(StreamEvent(kind="reasoning_delta", text="inspect first"))
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events], ["token", "reasoning"])
    def test_config_and_trace_paths_are_detected_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            self.assertTrue(_is_inside(workspace / ".env", workspace))
            self.assertFalse(_is_inside(Path(temporary) / ".env", workspace))

    def test_cli_refuses_credential_file_in_editable_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            env_file = workspace / ".env"
            env_file.write_text("SEECODER_API_KEY=test\nSEECODER_MODEL=fake\n", encoding="utf-8")
            with redirect_stderr(StringIO()):
                exit_code = main(["run", "do nothing", "--workspace", str(workspace), "--env-file", str(env_file)])
        self.assertEqual(exit_code, 2)

    def test_event_json_reports_configuration_errors_without_terminal_formatting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            stdout = StringIO()
            with patch.dict(
                "os.environ", {"SEECODER_API_KEY": "", "OPENAI_API_KEY": "", "SEECODER_MODEL": ""}, clear=False
            ), redirect_stdout(stdout):
                exit_code = main(
                    [
                        "run",
                        "do nothing",
                        "--workspace",
                        str(workspace),
                        "--env-file",
                        str(Path(temporary) / "missing.env"),
                        "--event-json",
                    ]
                )
        self.assertEqual(exit_code, 2)
        event = json.loads(stdout.getvalue())
        self.assertEqual(event["event"], "configuration_error")
        self.assertIn("SEECODER_API_KEY", event["data"]["message"])
