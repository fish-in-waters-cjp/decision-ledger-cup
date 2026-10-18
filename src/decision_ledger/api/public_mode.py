"""Public mode (DL-018): the showcase project is read-only and projects are created only as sandboxes."""

from __future__ import annotations

from fastapi import FastAPI, Request

from decision_ledger.api.errors import problem

READ_METHODS = {"GET", "HEAD", "OPTIONS"}
READ_ONLY = "The showcase is read-only. Start your own sandbox to try it."
# With Google sign-in on (DL-032) there are no anonymous sandboxes; the way in is signing in.
READ_ONLY_SIGN_IN = "The showcase is read-only. Sign in with Google to get your own workspace."


def install_public_mode(app: FastAPI, showcase_id: str | None, *, sign_in: bool = False) -> None:
    """Reject writes to the showcase and direct project creation with 403 problem responses."""
    prefix = f"/api/projects/{showcase_id}/" if showcase_id else None
    detail = READ_ONLY_SIGN_IN if sign_in else READ_ONLY

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path, method = request.url.path, request.method
        if path.rstrip("/") == "/api/projects" and method == "POST":
            return problem(403, "public-mode", "Projects are created as sandboxes in the public demo", detail)
        if prefix and path.startswith(prefix) and method not in READ_METHODS:
            return problem(403, "read-only-showcase", "The showcase project is read-only", detail)
        return await call_next(request)
