"""Daily LLM call limits (DL-021): every model call is counted per sandbox (project) and site-wide."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from pydantic import BaseModel

from decision_ledger.domain.errors import UsageLimitExceeded
from decision_ledger.domain.ports import LLMClient, UsageCounter

M = TypeVar("M", bound=BaseModel)

SANDBOX = "sandbox"
SITE = "site"


def usage_day() -> str:
    """Today's UTC date (``YYYY-MM-DD``); counters restart when it changes."""
    return datetime.now(UTC).date().isoformat()


class LimitedLLM:
    """An ``LLMClient`` that counts each call and refuses, without calling the model, once a limit is passed."""

    def __init__(
        self,
        inner: LLMClient,
        counter: UsageCounter,
        *,
        project_id: str,
        sandbox_limit: int | None,
        site_limit: int,
        today: Callable[[], str] = usage_day,
    ):
        self.inner = inner
        self.counter = counter
        self.project_id = project_id
        self.sandbox_limit = sandbox_limit
        self.site_limit = site_limit
        self.today = today
        self.name = inner.name

    def generate_json(self, prompt: str, response_model: type[M], *, key: str) -> M:
        day = self.today()
        # ``sandbox_limit=None`` counts the call site-wide only (the showcase rebuild).
        if self.sandbox_limit is not None and self.counter.increment(SANDBOX, f"{self.project_id}:{day}") > self.sandbox_limit:
            raise UsageLimitExceeded(SANDBOX, self.sandbox_limit)
        if self.counter.increment(SITE, day) > self.site_limit:
            raise UsageLimitExceeded(SITE, self.site_limit)
        return self.inner.generate_json(prompt, response_model, key=key)
