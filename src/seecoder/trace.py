"""Append-only, redacted JSONL execution traces."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    return value


class TraceWriter:
    """Store full run evidence locally while avoiding known credential values."""

    def __init__(self, directory: Path, *, secrets: tuple[str, ...] = ()) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.path = directory / f"{run_id}.jsonl"
        self._secrets = secrets

    def record(self, event: str, data: dict[str, Any]) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "data": _redact(data, self._secrets),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str) + "\n")


class NullTraceWriter:
    """Test-friendly trace sink with the same small interface."""

    path: None = None

    def record(self, event: str, data: dict[str, Any]) -> None:  # noqa: ARG002
        return
