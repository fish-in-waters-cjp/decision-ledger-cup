"""Fake LLM backend: replays recorded responses by request key (demo and fallback)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)


class FakeReplayLLM:
    """Looks up ``key`` in every ``*.json`` file under ``fixtures_dir``; unknown keys get an empty response."""

    name = "fake"

    def __init__(self, fixtures_dir: str | Path):
        self.responses: dict[str, object] = {}
        directory = Path(fixtures_dir)
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                self.responses.update(json.loads(path.read_text(encoding="utf-8")))

    def generate_json(self, prompt: str, response_model: type[M], *, key: str) -> M:
        if key not in self.responses:
            return response_model()
        return response_model.model_validate(self.responses[key])
