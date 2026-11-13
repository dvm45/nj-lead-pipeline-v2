"""
Per-run token usage tracker.

Accumulates input/output token counts by model family (haiku, opus, sonnet)
so the runner can report cost estimates in last_run.txt.

Usage:
    from njlead.token_tracker import tracker
    tracker.reset()
    tracker.record("opus", input_tokens=5000, output_tokens=1200)
    tracker.record("opus", input_tokens=4800, output_tokens=800, empty=True)
    summary = tracker.summary()
    # {'opus': {'input': 9800, 'output': 2000, 'calls': 2, 'empty_calls': 1}}
"""

from __future__ import annotations

import threading


class TokenTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, int]] = {}

    def reset(self) -> None:
        with self._lock:
            self._data.clear()

    def record(
        self,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        empty: bool = False,
    ) -> None:
        """Record tokens from one API call."""
        with self._lock:
            if model not in self._data:
                self._data[model] = {"input": 0, "output": 0, "calls": 0, "empty_calls": 0}
            self._data[model]["input"] += input_tokens
            self._data[model]["output"] += output_tokens
            self._data[model]["calls"] += 1
            if empty:
                self._data[model]["empty_calls"] += 1

    def summary(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}


# Module-level singleton
tracker = TokenTracker()
