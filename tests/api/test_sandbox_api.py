"""DL-018: sandbox routes, sandbox pages and public mode over the real SQLite store."""

import re

import pytest
from fastapi.testclient import TestClient

from decision_ledger.api.app import STATIC_DIR, create_app
from decision_ledger.bootstrap import Settings, build_provider, build_runtime, run_demo

SHOWCASE_WRITES = [
    ("post", "/sources", {"data": {"kind": "transcript"}, "files": {"file": ("a.txt", b"[00:00:01] A: hi")}}),
    ("post", "/ask", {"json": {"question": "why?"}}),
    ("post", "/pr-check", {"json": {"repo": "a/b", "pr_number": 1, "head_sha": "x", "title": "t", "author": "a", "diff": ""}}),
    ("post", "/alerts/A1/resolve", {"json": {"action": "dismiss", "note": ""}}),
    ("post", "/topics/T1/rename", {"json": {"name": "x"}}),
]


def app(tmp_path, public):
    env = {"DL_DB_PATH": str(tmp_path / "a.db"), "DL_PUBLIC_MODE": "1" if public else "0"}
    settings = Settings.from_env(env)
    provider, showcase = build_provider(settings)
    runtime = build_runtime(settings, showcase_project=showcase.id)
    return TestClient(create_app(provider, runtime)), provider, showcase


def assert_problem(r, status):
    assert r.status_code == status and r.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("public", [True, False])
def test_two_sandboxes_are_isolated(tmp_path, public):
    client, provider, _ = app(tmp_path, public)
    a, b = client.post("/api/sandboxes").json(), client.post("/api/sandboxes").json()
    assert re.fullmatch(r"[A-Za-z0-9_-]{16}", a["id"]) and a["id"] != b["id"] and a["url"] == f"/s/{a['id']}/"
    run_demo(provider.for_project(a["id"]))
    assert client.get(f"/api/projects/{a['id']}/decisions").json()
    assert client.get(f"/api/projects/{b['id']}/decisions").json() == []
    assert client.get(f"/api/projects/{b['id']}/alerts").json() == []
    assert client.get(f"/api/sandboxes/{a['id']}").json()["id"] == a["id"]


def test_reset_only_clears_own_sandbox(tmp_path):
    client, provider, _ = app(tmp_path, True)
    a, b = client.post("/api/sandboxes").json()["id"], client.post("/api/sandboxes").json()["id"]
    run_demo(provider.for_project(a))
    run_demo(provider.for_project(b))
    assert client.post(f"/api/sandboxes/{a}/reset").status_code == 204
    assert client.get(f"/api/projects/{a}/decisions").json() == []
    assert client.get(f"/api/projects/{b}/decisions").json()


def test_unknown_and_expired_sandboxes(tmp_path):
    client, provider, showcase = app(tmp_path, True)
    r = client.get("/api/sandboxes/" + "z" * 16)
    assert_problem(r, 404)
    assert r.json()["detail"] == "Sandbox expired, start a new one"
    provider.sandboxes.registry.register("y" * 16)
    r = client.get("/api/sandboxes/" + "y" * 16)
    assert_problem(r, 410)
    assert r.json()["detail"] == "Sandbox expired, start a new one"
    assert_problem(client.post(f"/api/sandboxes/{showcase.id}/reset"), 404)


def test_sandbox_page_serves_the_web_ui(tmp_path):
    client, *_ = app(tmp_path, True)
    sid = client.post("/api/sandboxes").json()["id"]
    for path in (f"/s/{sid}", f"/s/{sid}/", f"/s/{sid}/anything"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and 'id="view"' in r.text


def test_public_mode_lists_only_showcase_and_blocks_project_creation(tmp_path):
    client, _, showcase = app(tmp_path, True)
    client.post("/api/sandboxes")
    assert [p["id"] for p in client.get("/api/projects").json()] == [showcase.id]
    assert_problem(client.post("/api/projects", json={"name": "mine"}), 403)


@pytest.mark.parametrize("method,suffix,kwargs", SHOWCASE_WRITES)
def test_public_mode_showcase_is_read_only(tmp_path, method, suffix, kwargs):
    client, provider, showcase = app(tmp_path, True)
    before = len(provider.store.for_project(showcase.id).list_sources())
    r = getattr(client, method)(f"/api/projects/{showcase.id}{suffix}", **kwargs)
    assert_problem(r, 403)
    assert len(provider.store.for_project(showcase.id).list_sources()) == before
    assert client.get(f"/api/projects/{showcase.id}/overview").status_code == 200


def test_private_mode_is_unchanged(tmp_path):
    client, _, showcase = app(tmp_path, False)
    assert client.post("/api/projects", json={"name": "mine"}).status_code == 201
    assert len(client.get("/api/projects").json()) == 2
    r = client.post(f"/api/projects/{showcase.id}/topics/T9/rename", json={"name": "x"})
    assert r.status_code == 404  # reaches the service instead of the public-mode guard


def test_web_ui_assets_are_absolute_and_sandbox_strings_present():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'href="/styles.css"' in html and 'src="/app.js"' in html
    from tests.ui_text import shows

    for text in ("Start my sandbox", "Reset my sandbox", "Sandbox expired, start a new one"):
        assert shows(STATIC_DIR / "sandbox.js", text), text  # DL-031: literal or its en.js key


def test_daily_sandbox_limit_returns_429_problem(tmp_path):
    env = {"DL_DB_PATH": str(tmp_path / "l.db"), "DL_PUBLIC_MODE": "1", "DL_SANDBOX_DAILY_CREATE_LIMIT": "1"}
    settings = Settings.from_env(env)
    assert settings.sandbox_daily_create_limit == 1 and Settings.from_env({}).sandbox_daily_create_limit == 100
    provider, showcase = build_provider(settings)
    client = TestClient(create_app(provider, build_runtime(settings, showcase_project=showcase.id)))
    assert client.post("/api/sandboxes").status_code == 201
    projects = len(provider.store.list_projects())
    r = client.post("/api/sandboxes")
    assert_problem(r, 429)
    assert r.json()["type"] == "/problems/sandbox-limit" and r.json()["title"] == "Daily sandbox limit reached, try tomorrow"
    assert len(provider.store.list_projects()) == projects


def test_sandbox_js_shows_the_limit_message():
    from tests.ui_text import shows

    js = (STATIC_DIR / "sandbox.js").read_text(encoding="utf-8")
    assert shows(STATIC_DIR / "sandbox.js", "Daily sandbox limit reached, try tomorrow") and "429" in js  # DL-031
