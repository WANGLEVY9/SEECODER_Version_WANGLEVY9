"""Configuration loading with explicit validation and no credential logging."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from seecoder.types import Mode


class ConfigError(ValueError):
    """Raised when required runtime configuration is missing or unsafe."""


def load_env_file(path: Path) -> dict[str, str]:
    """Read a deliberately small dotenv subset without adding a runtime dependency.

    Existing process environment variables always take precedence. The parser accepts
    `NAME=value`, ignores blank/comment lines, and handles one matching quote pair.
    It intentionally does not perform variable expansion or command substitution.
    """

    if not path.exists():
        return {}
    if not path.is_file():
        raise ConfigError(f"Environment file is not a regular file: {path}")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise ConfigError(f"Invalid environment entry at {path}:{line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            raise ConfigError(f"Invalid environment variable name at {path}:{line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    max_steps: int = 16
    max_consecutive_tool_errors: int = 4
    model_retries: int = 3
    context_char_budget: int = 45_000
    command_timeout_s: int = 30
    command_max_timeout_s: int = 120
    allow_dangerous_commands: bool = False
    execution_mode: str = "restricted"
    thinking_mode: str = "provider_default"
    reasoning_effort: str = "high"
    mode: Mode = Mode.AUTO
    compaction_enabled: bool = False

    @classmethod
    def from_environment(
        cls,
        *,
        env_file: Path | None = None,
        max_steps: int | None = None,
        allow_dangerous_commands: bool = False,
        execution_mode: str | None = None,
    ) -> Settings:
        file_values = load_env_file(env_file) if env_file else {}

        def value(name: str, default: str | None = None) -> str | None:
            return os.environ.get(name, file_values.get(name, default))

        api_key = value("SEECODER_API_KEY") or value("OPENAI_API_KEY")
        model = value("SEECODER_MODEL")
        missing = [name for name, item in (("SEECODER_API_KEY", api_key), ("SEECODER_MODEL", model)) if not item]
        if missing:
            raise ConfigError("Missing required configuration: " + ", ".join(missing))

        try:
            configured_max_steps = max_steps if max_steps is not None else int(value("SEECODER_MAX_STEPS", "16") or "16")
        except ValueError as error:
            raise ConfigError("SEECODER_MAX_STEPS must be an integer") from error
        if not 1 <= configured_max_steps <= 100:
            raise ConfigError("SEECODER_MAX_STEPS must be between 1 and 100")

        base_url = value("SEECODER_BASE_URL", "https://api.openai.com/v1")
        if not base_url or not base_url.startswith(("https://", "http://")):
            raise ConfigError("SEECODER_BASE_URL must be an HTTP(S) URL")

        configured_execution_mode = execution_mode or value("SEECODER_EXECUTION_MODE", "restricted") or "restricted"
        if configured_execution_mode not in {"restricted", "host_shell"}:
            raise ConfigError("SEECODER_EXECUTION_MODE must be restricted or host_shell")

        thinking_mode = value("SEECODER_THINKING_MODE", "provider_default") or "provider_default"
        if thinking_mode not in {"provider_default", "enabled", "disabled"}:
            raise ConfigError("SEECODER_THINKING_MODE must be provider_default, enabled, or disabled")
        reasoning_effort = value("SEECODER_REASONING_EFFORT", "high") or "high"
        if reasoning_effort not in {"low", "high", "max"}:
            raise ConfigError("SEECODER_REASONING_EFFORT must be low, high, or max")

        mode_name = value("SEECODER_MODE", "auto") or "auto"
        if mode_name not in {"auto", "plan", "ask"}:
            raise ConfigError("SEECODER_MODE must be auto, plan, or ask")

        compaction = (value("SEECODER_COMPACTION", "0") or "0").strip().lower()
        if compaction not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise ConfigError("SEECODER_COMPACTION must be 0/1 or on/off")

        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url.rstrip("/"),
            max_steps=configured_max_steps,
            allow_dangerous_commands=allow_dangerous_commands,
            execution_mode=configured_execution_mode,
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
            mode=Mode(mode_name),
            compaction_enabled=compaction in {"1", "true", "yes", "on"},
        )
