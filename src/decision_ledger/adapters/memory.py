"""In-memory SandboxRegistry and UsageCounter for local runs and tests (lost on restart)."""

from __future__ import annotations

import threading


class InMemoryUsageCounter:
    """Implements ``UsageCounter`` with a dict guarded by a lock."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def increment(self, scope: str, key: str) -> int:
        with self._lock:
            value = self._counts.get((scope, key), 0) + 1
            self._counts[(scope, key)] = value
            return value

    def get(self, scope: str, key: str) -> int:
        with self._lock:
            return self._counts.get((scope, key), 0)


class InMemorySandboxRegistry:
    """Implements ``SandboxRegistry`` with a set guarded by a lock."""

    def __init__(self) -> None:
        self._ids: set[str] = set()
        self._lock = threading.Lock()

    def register(self, sandbox_id: str) -> None:
        with self._lock:
            self._ids.add(sandbox_id)

    def exists(self, sandbox_id: str) -> bool:
        with self._lock:
            return sandbox_id in self._ids
