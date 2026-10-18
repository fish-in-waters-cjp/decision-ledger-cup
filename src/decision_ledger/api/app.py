"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from collections.abc import Sequence

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from decision_ledger.api.errors import internal_error, register_error_handlers
from decision_ledger.api.public_mode import install_public_mode
from decision_ledger.api.routes import router
from decision_ledger.services.container import ContainerProvider, RuntimeInfo

PACKAGE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = PACKAGE_DIR / "web" / "static"
DEMO_DIR = PACKAGE_DIR / "demo" / "fixtures"


def create_app(
    provider: ContainerProvider, runtime: RuntimeInfo | None = None, cors_origins: Sequence[str] = ()
) -> FastAPI:
    """Build the app around a per-project service provider; the web UI is served at ``/``."""
    runtime = runtime or RuntimeInfo()
    app = FastAPI(title="Decision Ledger", version=runtime.version)
    app.state.provider = provider
    app.state.runtime = runtime
    register_error_handlers(app)

    # Registered before the StaticFiles mount at "/", which would otherwise answer every unknown path.
    # /api/health is the same probe for platforms that reserve paths ending in "z".
    @app.get("/healthz", include_in_schema=False)
    @app.get("/api/health", include_in_schema=False)
    def healthz() -> dict[str, object]:
        return runtime.health()

    app.include_router(router)

    # A sandbox page is the same single-page UI; it reads its sandbox id from the path.
    @app.get("/s/{sandbox_id}", include_in_schema=False)
    @app.get("/s/{sandbox_id}/{rest:path}", include_in_schema=False)
    def sandbox_page(sandbox_id: str, rest: str = "") -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    if runtime.public_mode:
        install_public_mode(app, runtime.showcase_project, sign_in=runtime.auth_mode != "none")

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        # Force ETag revalidation so an upgraded server never runs stale UI code.
        response = await call_next(request)
        if not request.url.path.startswith(("/api/", "/webhook/", "/webhooks/")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def unhandled_errors(request: Request, call_next):
        # Starlette turns an uncaught exception into a 500 in ServerErrorMiddleware, outside CORS, so the browser
        # on the direct Cloud Run URL could not read it (R-API-1). Catching it here, inside CORS, returns the same
        # problem response with the CORS headers; the traceback goes to the log only.
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 - last-resort handler
            return internal_error(exc)

    if cors_origins:
        # Added last so it is the outermost middleware: rejections from the public-mode guard (403) and other
        # middleware carry the CORS headers too, which the browser needs on the direct Cloud Run URL (DL-030).
        # Only the listed origins (the Hosting URL) may call the API cross-origin; none when unset.
        app.add_middleware(CORSMiddleware, allow_origins=list(cors_origins), allow_methods=["*"], allow_headers=["*"])
    if DEMO_DIR.is_dir():
        # The web UI's demo script uploads these sample sources through the normal import API.
        app.mount("/demo", StaticFiles(directory=DEMO_DIR), name="demo")
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="web")
    return app
