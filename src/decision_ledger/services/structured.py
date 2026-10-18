"""Structured LLM calls: every response is validated against a Pydantic model."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, ValidationError

from decision_ledger.domain.errors import LLMOutputError, LLMTransportError, LLMUnavailableError
from decision_ledger.domain.ports import LLMClient

M = TypeVar("M", bound=BaseModel)

MAX_ATTEMPTS = 2


def call_structured(llm: LLMClient, prompt: str, response_model: type[M], *, key: str) -> M:
    """Call the LLM and validate its output, retrying once on invalid output or transport failure."""
    last_error: Exception | None = None
    for _ in range(MAX_ATTEMPTS):
        try:
            result = llm.generate_json(prompt, response_model, key=key)
            if not isinstance(result, response_model):
                result = response_model.model_validate(result)
            return result
        except (ValidationError, LLMTransportError) as exc:
            last_error = exc
    error_type = LLMUnavailableError if isinstance(last_error, LLMTransportError) else LLMOutputError
    raise error_type(f"{response_model.__name__} failed after {MAX_ATTEMPTS} attempts: {last_error}") from last_error
