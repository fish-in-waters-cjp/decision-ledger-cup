"""Map errors to RFC 7807 problem responses (type / title / status / detail)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from decision_ledger.domain.errors import (
    AlertAlreadyResolved,
    DecisionLedgerError,
    DiffUnavailable,
    InvalidProjectOperation,
    InvalidResolution,
    InvalidTopicOperation,
    SandboxExpired,
    SandboxLimitExceeded,
    Unauthenticated,
    Forbidden,
    ConcurrentModification,
    UserLimitExceeded,
    LLMOutputError,
    LLMUnavailableError,
    NotFound,
    ParseError,
    ReplayNotReady,
    UsageLimitExceeded,
)

log = logging.getLogger(__name__)

PROBLEM_TYPES: list[tuple[type[DecisionLedgerError], int, str, str]] = [
    (ParseError, 422, "parse-error", "Source could not be parsed"),
    (InvalidResolution, 422, "invalid-resolution", "This resolution does not apply to this alert"),
    (InvalidTopicOperation, 422, "invalid-topic-operation", "This topic cannot be changed"),
    (InvalidProjectOperation, 422, "invalid-project-operation", "This project cannot be created"),
    (DiffUnavailable, 422, "diff-unavailable", "PR diff unavailable"),
    (AlertAlreadyResolved, 409, "alert-already-resolved", "Alert already resolved"),
    (SandboxLimitExceeded, 429, "sandbox-limit", "Daily sandbox limit reached, try tomorrow"),
    (Unauthenticated, 401, "unauthenticated", "Sign in with Google to continue"),
    (Forbidden, 403, "forbidden", "Not allowed for this account"),
    (ConcurrentModification, 409, "concurrent-modification", "Someone else changed this project at the same time; please retry"),
    (UserLimitExceeded, 429, "user-limit", "Today's new account limit is reached, try tomorrow"),
    (SandboxExpired, 410, "sandbox-expired", "Sandbox expired, start a new one"),
    (ReplayNotReady, 409, "replay-not-ready", "Import the demo sources before replaying a PR"),
    (NotFound, 404, "not-found", "Not found"),
    (UsageLimitExceeded, 429, "rate-limited", "Daily AI usage limit reached"),
    # Subclass first: isinstance matching stops at the first hit.
    (LLMUnavailableError, 502, "llm-unavailable", "AI model call failed"),
    (LLMOutputError, 502, "llm-output-invalid", "AI model returned an invalid response"),
]


class ProblemError(Exception):
    """An API-level problem raised directly by a route."""

    def __init__(self, status: int, slug: str, title: str, detail: str = ""):
        super().__init__(detail or title)
        self.status, self.slug, self.title, self.detail = status, slug, title, detail


def problem(status: int, slug: str, title: str, detail: str = "") -> JSONResponse:
    """Build an ``application/problem+json`` response."""
    return JSONResponse(
        {"type": f"/problems/{slug}", "title": title, "status": status, "detail": detail},
        status_code=status,
        media_type="application/problem+json",
    )


def problem_for(exc: DecisionLedgerError) -> JSONResponse:
    """Translate a domain error to its problem response (400 when unmapped)."""
    for error_type, status, slug, title in PROBLEM_TYPES:
        if isinstance(exc, error_type):
            return problem(status, slug, title, str(exc))
    return problem(400, "bad-request", "Request could not be processed", str(exc))


def internal_error(exc: Exception) -> JSONResponse:
    """Log an unexpected error with its traceback and answer a generic 500 problem (no internals leak)."""
    log.exception("unhandled error", exc_info=exc)
    return problem(500, "internal-error", "Internal server error", "")


def register_error_handlers(app: FastAPI) -> None:
    """Install handlers so every error leaves the API as a problem response."""

    @app.exception_handler(DecisionLedgerError)
    async def _domain(_: Request, exc: DecisionLedgerError) -> JSONResponse:
        return problem_for(exc)

    @app.exception_handler(ProblemError)
    async def _problem(_: Request, exc: ProblemError) -> JSONResponse:
        return problem(exc.status, exc.slug, exc.title, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(422, "validation-error", "Invalid request fields", str(exc.errors()))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        return internal_error(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, "http-error", str(exc.detail), "")
