"""Recording LLM backend: forwards to a real backend and saves every response as a Fake fixture."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from decision_ledger.domain.ports import LLMClient

M = TypeVar("M", bound=BaseModel)


class RecordingBudgetExceeded(RuntimeError):
    """Raised before an inner call once ``max_calls`` calls have been made."""


class RecordingLLM:
    """Wraps ``inner`` and writes ``{key: response}`` to ``path`` after every successful call.

    The file has the shape :class:`FakeReplayLLM` reads (``model_dump(mode="json")``
    per key), so a run recorded against Gemini replays offline with the Fake
    backend. Existing entries are kept and a repeated key takes the latest response.
    ``calls`` counts every attempt that reached the inner backend, failures included;
    with ``max_calls`` set, the next attempt raises :class:`RecordingBudgetExceeded`
    without calling the inner backend.
    """

    def __init__(self, inner: LLMClient, path: str | Path, *, max_calls: int | None = None):
        self.inner = inner
        self.path = Path(path)
        self.name = inner.name
        self.max_calls = max_calls
        self.calls = 0
        self._lock = threading.Lock()
        self._responses: dict[str, object] = (
            json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        )

    def generate_json(self, prompt: str, response_model: type[M], *, key: str) -> M:
        with self._lock:
            if self.max_calls is not None and self.calls >= self.max_calls:
                raise RecordingBudgetExceeded(f"recording budget of {self.max_calls} LLM calls is used up")
            self.calls += 1
        result = self.inner.generate_json(prompt, response_model, key=key)
        if not isinstance(result, response_model):
            result = response_model.model_validate(result)
        with self._lock:
            self._responses[key] = result.model_dump(mode="json")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._responses, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        return result
