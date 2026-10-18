"""Gemini backend via the google-genai SDK (optional ``gemini`` extra)."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from decision_ledger.domain.errors import LLMTransportError

M = TypeVar("M", bound=BaseModel)


class GeminiLLM:
    """Structured JSON output through ``response_schema``; works with an API key or Vertex AI."""

    name = "gemini"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        vertexai: bool = False,
        project: str | None = None,
        location: str | None = None,
        timeout: float = 60.0,
        access_token: str | None = None,
        client: Any = None,
    ):
        self.model = model
        self.timeout = timeout
        if client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise RuntimeError("Gemini backend needs the 'gemini' extra: uv sync --extra gemini") from exc
            if vertexai and access_token:
                # A short-lived OAuth token (e.g. `gcloud auth print-access-token`) instead of ADC.
                from google.oauth2.credentials import Credentials

                client = genai.Client(vertexai=True, project=project, location=location, credentials=Credentials(access_token))
            elif vertexai:
                client = genai.Client(vertexai=True, project=project, location=location)
            else:
                client = genai.Client(api_key=api_key)
        self.client = client

    def generate_json(self, prompt: str, response_model: type[M], *, key: str) -> M:
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "temperature": 0,
                    "response_schema": response_model,
                    "http_options": {"timeout": int(self.timeout * 1000)},
                },
            )
        except Exception as exc:  # SDK and network errors vary by transport
            raise LLMTransportError(f"Gemini request failed: {exc}") from exc
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, response_model):
            return parsed
        text = getattr(response, "text", None)
        if not text:
            raise LLMTransportError("Gemini returned an empty response")
        return response_model.model_validate_json(text)
