"""Accumulate model token usage across one agent run."""

from __future__ import annotations

from seecoder.types import Usage


class UsageTracker:
    """Sum Usage samples and expose the per-run totals without coupling to the runner."""

    def __init__(self) -> None:
        self._total = Usage(0, 0, 0)
        self._last: Usage | None = None
        self._calls = 0

    def record(self, usage: Usage | None) -> None:
        if usage is None:
            return
        self._total = self._total.plus(usage)
        self._last = usage
        self._calls += 1

    @property
    def total(self) -> Usage:
        return self._total

    @property
    def last(self) -> Usage | None:
        return self._last

    @property
    def calls(self) -> int:
        return self._calls
