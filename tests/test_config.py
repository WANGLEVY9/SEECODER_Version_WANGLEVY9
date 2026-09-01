from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from seecoder.config import ConfigError, Settings, load_env_file


class ConfigTests(unittest.TestCase):
    def test_default_step_budget_leaves_room_for_validation_and_summary(self) -> None:
        self.assertEqual(Settings(api_key="test-key", model="test-model").max_steps, 32)

    def test_env_file_and_process_environment_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text("SEECODER_API_KEY=file-key\nSEECODER_MODEL=file-model\n", encoding="utf-8")
            previous = os.environ.get("SEECODER_MODEL")
            os.environ["SEECODER_MODEL"] = "shell-model"
            try:
                settings = Settings.from_environment(env_file=path)
            finally:
                if previous is None:
                    del os.environ["SEECODER_MODEL"]
                else:
                    os.environ["SEECODER_MODEL"] = previous
        self.assertEqual(settings.api_key, "file-key")
        self.assertEqual(settings.model, "shell-model")

    def test_invalid_env_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text("invalid line", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_env_file(path)

    def test_invalid_thinking_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text(
                "SEECODER_API_KEY=file-key\nSEECODER_MODEL=file-model\nSEECODER_THINKING_MODE=maybe\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                Settings.from_environment(env_file=path)

    def test_invalid_execution_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text(
                "SEECODER_API_KEY=file-key\nSEECODER_MODEL=file-model\nSEECODER_EXECUTION_MODE=unsafe\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                Settings.from_environment(env_file=path)

    def test_model_timeout_is_configurable_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text(
                "SEECODER_API_KEY=file-key\nSEECODER_MODEL=file-model\nSEECODER_MODEL_TIMEOUT_S=45\n",
                encoding="utf-8",
            )
            settings = Settings.from_environment(env_file=path)
        self.assertEqual(settings.model_timeout_s, 45.0)
