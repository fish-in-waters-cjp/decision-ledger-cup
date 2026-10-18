"""DL-006: CORS only for the configured origins."""

from fastapi.testclient import TestClient

from decision_ledger.api.app import create_app
from decision_ledger.bootstrap import ContainerFactory
from tests.fakes import FakeLedgerStore, FakeLLM, FakePullRequestSource, RecordingPublisher

HOSTING = "https://decision-ledger-cup.web.app"


def client(origins=()):
    store = FakeLedgerStore()
    store.create_project("v2", None)
    provider = ContainerFactory(store, FakeLLM({}), RecordingPublisher(), FakePullRequestSource())
    return TestClient(create_app(provider, cors_origins=origins))


def preflight(c, origin):
    return c.options("/api/projects", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})


def test_listed_origin_is_allowed():
    r = preflight(client((HOSTING,)), HOSTING)
    assert r.headers.get("access-control-allow-origin") == HOSTING


def test_other_origin_gets_no_allow_header():
    c = client((HOSTING,))
    assert "access-control-allow-origin" not in preflight(c, "https://evil.example").headers
    assert "access-control-allow-origin" not in c.get("/api/projects", headers={"Origin": "https://evil.example"}).headers


def test_no_cors_headers_when_unconfigured():
    c = client()
    assert "access-control-allow-origin" not in c.get("/api/projects", headers={"Origin": HOSTING}).headers


def public_client(tmp_path, **env):
    from decision_ledger.bootstrap import Settings, build_provider, build_runtime

    settings = Settings.from_env({"DL_DB_PATH": str(tmp_path / "c.db"), "DL_PUBLIC_MODE": "1", **env})
    provider, showcase = build_provider(settings)
    return TestClient(create_app(provider, build_runtime(settings, showcase_project=showcase.id), cors_origins=(HOSTING,))), showcase


def test_middleware_rejections_carry_cors_headers(tmp_path):
    """DL-030 R-API-1: the browser must be able to read 403/429 from the direct Cloud Run URL."""
    c, showcase = public_client(tmp_path, DL_SANDBOX_DAILY_CREATE_LIMIT="0")
    origin = {"Origin": HOSTING}
    forbidden = c.post(f"/api/projects/{showcase.id}/ask", json={"question": "why?"}, headers=origin)
    assert forbidden.status_code == 403 and forbidden.headers.get("access-control-allow-origin") == HOSTING
    limited = c.post("/api/sandboxes", headers=origin)
    assert limited.status_code == 429 and limited.headers.get("access-control-allow-origin") == HOSTING
    missing = c.post("/api/projects/nope/ask", json={"question": "why?"}, headers=origin)
    assert missing.status_code == 404 and missing.headers.get("access-control-allow-origin") == HOSTING


def test_unhandled_errors_return_cors_enabled_problem(tmp_path, caplog):
    """R-API-1: a crash behind the direct Cloud Run URL must still be readable by the browser."""
    import logging

    c, _ = public_client(tmp_path)
    c = TestClient(c.app, raise_server_exceptions=False)

    from fastapi.routing import APIRoute

    def boom():
        raise RuntimeError("secret internals")

    c.app.router.routes.insert(0, APIRoute("/api/boom", boom))  # ahead of the StaticFiles mount at "/"

    with caplog.at_level(logging.ERROR):
        r = c.get("/api/boom", headers={"Origin": HOSTING})
    assert r.status_code == 500 and r.headers.get("access-control-allow-origin") == HOSTING
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "/problems/internal-error" and body["status"] == 500
    assert "secret internals" not in r.text and "Traceback" not in r.text
    assert any("secret internals" in (rec.exc_text or "") or rec.exc_info for rec in caplog.records)
