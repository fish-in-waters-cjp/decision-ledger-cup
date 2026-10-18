"""DL-032: signed-in workspaces over the HTTP API (fake identity tokens)."""

import json

import pytest
from fastapi.testclient import TestClient

from decision_ledger.adapters.firebase_auth import FakeIdentityVerifier
from decision_ledger.adapters.memory import InMemoryUsageCounter
from decision_ledger.api.app import create_app
from decision_ledger.api.routes import router
from decision_ledger.api.webhook_auth import sign
from decision_ledger.bootstrap import ContainerFactory
from decision_ledger.domain.errors import ConcurrentModification
from decision_ledger.domain.models import CheckableRule
from decision_ledger.services.container import RuntimeInfo
from decision_ledger.services.sandboxes import workspace_id
from tests.factories import decision
from tests.fakes import FakeLedgerStore, FakeLLM, FakePullRequestSource, RecordingPublisher

SECRET = "s3cret"
TRANSCRIPT = "[00:08:12] Alice: 那 v2 就定 10/15 上線。"
DIFF = """diff --git a/src/report/service.py b/src/report/service.py
--- a/src/report/service.py
+++ b/src/report/service.py
@@ -9,1 +9,2 @@
 from app.api import internal_client
+from app.db import db_client
"""
ALICE = {"Authorization": "Bearer fake:alice"}
BOB = {"Authorization": "Bearer fake:bob"}


def build(responses=None, *, public_mode=True, new_user_limit=100, usage=None, sandbox_llm_limit=60):
    store, llm = FakeLedgerStore(), FakeLLM(responses or {})
    showcase = store.create_project("Showcase", "northwind/v2")
    provider = ContainerFactory(
        store, llm, RecordingPublisher(), FakePullRequestSource(), webhook_secret=SECRET,
        identity=FakeIdentityVerifier(), auth_required=True, daily_new_user_limit=new_user_limit,
        usage=usage, sandbox_daily_llm_limit=sandbox_llm_limit,
    )
    provider.projects.showcase_id = provider.sandboxes.showcase_id = showcase.id
    runtime = RuntimeInfo(public_mode=public_mode, showcase_project=showcase.id, auth_mode="fake",
                          firebase={"apiKey": "k", "authDomain": "demo.firebaseapp.com", "projectId": "demo"})
    return TestClient(create_app(provider, runtime)), store, llm, showcase


def problem(response, status, slug=None):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    if slug:
        assert response.json()["type"] == f"/problems/{slug}"


def me(client, headers=ALICE):
    response = client.get("/api/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["project"]["id"]


def upload(client, pid, headers):
    return client.post(f"/api/projects/{pid}/sources", data={"kind": "transcript"},
                       files={"file": ("t.txt", TRANSCRIPT.encode(), "text/plain")}, headers=headers)


# --- /api/me -----------------------------------------------------------------


def test_me_creates_the_workspace_once_and_returns_it_after():
    client, store, _, _ = build()
    first = client.get("/api/me", headers=ALICE).json()
    assert first["uid"] == "alice" and first["project"]["owner_uid"] == "alice"
    assert first["project"]["id"] == workspace_id("alice") and me(client) == first["project"]["id"]
    assert len(store.projects) == 2


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic fake:alice"}])
def test_me_without_a_valid_token_is_401(headers):
    client, store, _, _ = build()
    problem(client.get("/api/me", headers=headers), 401, "unauthenticated")
    assert len(store.projects) == 1


def test_new_users_over_the_daily_limit_get_429():
    client, store, _, _ = build(new_user_limit=1, usage=InMemoryUsageCounter())
    me(client)
    problem(client.get("/api/me", headers=BOB), 429, "user-limit")
    assert me(client) == workspace_id("alice")


# --- access control ----------------------------------------------------------


def test_owner_writes_their_workspace_others_are_forbidden_anonymous_unauthenticated():
    client, store, llm, _ = build()
    pid = me(client)
    assert upload(client, pid, ALICE).status_code == 200
    problem(upload(client, pid, BOB), 403, "forbidden")
    problem(upload(client, pid, {}), 401, "unauthenticated")
    assert len(store.for_project(pid).sources) == 1


@pytest.mark.parametrize("path", ["overview", "decisions", "topics", "alerts", "pr-checks", "topics/T1", "sources/S1"])
def test_reading_someone_elses_workspace_is_forbidden(path):
    client, _, _, _ = build()
    pid = me(client)
    assert client.get(f"/api/projects/{pid}/{path}", headers=ALICE).status_code in (200, 404)  # 404: no such topic yet
    problem(client.get(f"/api/projects/{pid}/{path}", headers=BOB), 403)
    problem(client.get(f"/api/projects/{pid}/{path}"), 401)


def test_every_write_route_checks_ownership():
    client, store, _, _ = build()
    pid = me(client)
    store.for_project(pid).decisions["1"] = decision("1", project_id=pid)
    writes = [
        ("post", f"/api/projects/{pid}/pr-check", {"json": {"diff": DIFF}}),
        ("post", f"/api/projects/{pid}/ask", {"json": {"question": "why"}}),
        ("post", f"/api/projects/{pid}/alerts/1/resolve", {"json": {"action": "reconsider"}}),
        ("post", f"/api/projects/{pid}/decisions/1/reaffirm", {"json": {"note": ""}}),
        ("post", f"/api/projects/{pid}/topics/T1/rename", {"json": {"name": "x"}}),
        ("post", f"/api/projects/{pid}/topics/T1/merge", {"json": {"into": "T2"}}),
        ("post", f"/api/projects/{pid}/topics/T1/move", {"json": {"decision_ids": ["1"], "into": "T2"}}),
        ("post", f"/api/projects/{pid}/topics/T1/split", {"json": {"decision_ids": ["1"], "name": "y"}}),
        ("patch", f"/api/projects/{pid}", {"json": {"repo": "acme/demo"}}),
    ]
    for method, url, kwargs in writes:
        problem(getattr(client, method)(url, headers=BOB, **kwargs), 403)
        problem(getattr(client, method)(url, **kwargs), 401)
    assert store.get_project(pid).repo is None


def test_showcase_is_readable_without_sign_in_and_never_writable():
    client, store, _, showcase = build(public_mode=False)  # authorize() guards it even without the public-mode middleware
    assert client.get(f"/api/projects/{showcase.id}/overview").status_code == 200
    for headers in ({}, ALICE):
        problem(upload(client, showcase.id, headers), 403)
        problem(client.patch(f"/api/projects/{showcase.id}", json={"repo": "a/b"}, headers=headers), 403)
    assert store.for_project(showcase.id).sources == {}


def test_project_list_shows_the_showcase_and_only_my_workspace():
    client, _, _, showcase = build()
    assert [p["id"] for p in client.get("/api/projects", headers={"Authorization": "Bearer fake:"}).json()] == [showcase.id]
    alice, bob = me(client), me(client, BOB)
    assert [p["id"] for p in client.get("/api/projects").json()] == [showcase.id]
    assert [p["id"] for p in client.get("/api/projects", headers=ALICE).json()] == [showcase.id, alice]
    assert bob not in [p["id"] for p in client.get("/api/projects", headers=ALICE).json()]


def test_projects_and_anonymous_sandboxes_cannot_be_created_when_sign_in_is_on():
    client, store, _, _ = build(public_mode=False)
    problem(client.post("/api/projects", json={"name": "x"}, headers=ALICE), 403)
    problem(client.post("/api/sandboxes"), 403)
    problem(client.get("/api/sandboxes/abcdefghijklmnop"), 403)
    problem(client.post("/api/sandboxes/abcdefghijklmnop/reset"), 403)
    assert len(store.projects) == 1


def test_reset_clears_only_my_workspace():
    client, store, _, _ = build()
    alice, bob = me(client), me(client, BOB)
    assert upload(client, alice, ALICE).status_code == 200 and upload(client, bob, BOB).status_code == 200
    assert client.post("/api/me/reset", headers=ALICE).status_code == 204
    assert store.for_project(alice).sources == {} and len(store.for_project(bob).sources) == 1
    problem(client.post("/api/me/reset"), 401)


def test_patch_sets_and_validates_the_repo():
    client, store, _, _ = build()
    pid = me(client)
    response = client.patch(f"/api/projects/{pid}", json={"repo": "acme/demo-repo"}, headers=ALICE)
    assert response.status_code == 200 and response.json()["repo"] == "acme/demo-repo"
    problem(client.patch(f"/api/projects/{pid}", json={"repo": "not a repo"}, headers=ALICE), 422)
    assert client.patch(f"/api/projects/{pid}", json={"repo": None}, headers=ALICE).json()["repo"] is None


def test_concurrent_modification_is_409():
    client, store, _, _ = build()
    pid = me(client)
    repo = store.for_project(pid)

    def boom():
        raise ConcurrentModification("another change was saved to this project at the same time; please retry")

    repo.unit_of_work = boom
    problem(upload(client, pid, ALICE), 409, "concurrent-modification")


def test_a_signed_in_users_llm_calls_are_capped_per_workspace():
    client, store, llm, _ = build(usage=InMemoryUsageCounter(), sandbox_llm_limit=0)
    pid = me(client)
    problem(upload(client, pid, ALICE), 429, "rate-limited")
    assert llm.calls == [] and store.for_project(pid).sources == {}


# --- runtime and webhook -----------------------------------------------------


def test_runtime_exposes_the_auth_mode_and_firebase_web_config():
    client, _, _, showcase = build()
    body = client.get("/api/runtime").json()
    assert body["auth_mode"] == "fake" and body["firebase"]["projectId"] == "demo"
    assert body["showcase_project"] == showcase.id and body["public_mode"] is True


def post_webhook(client, repo):
    payload = {"action": "opened", "number": 87, "repository": {"full_name": repo},
               "pull_request": {"number": 87, "title": "t", "user": {"login": "k"}, "head": {"sha": "abc"}}, "diff": DIFF}
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(SECRET, body), "Content-Type": "application/json"}
    return client.post("/webhook/github", content=body, headers=headers)


def test_webhook_reaches_every_project_registering_the_repo():
    responses = {"DeviationJudgement": {"violations": [
        {"decision_id": "23", "file": "src/report/service.py", "line": 10, "snippet": "from app.db import db_client"}]}}
    client, store, llm, showcase = build(responses)
    alice, bob = me(client), me(client, BOB)
    for pid, headers in ((alice, ALICE), (bob, BOB)):
        client.patch(f"/api/projects/{pid}", json={"repo": "acme/demo"}, headers=headers)
        store.for_project(pid).decisions["23"] = decision(
            "23", project_id=pid, statement="report-service goes through the internal API",
            rule=CheckableRule(description="r", path_patterns=["src/report/*"]))
    response = post_webhook(client, "acme/demo")
    assert response.status_code == 202 and response.json() == {"queued": "acme/demo#87@abc", "projects": 2}
    assert all(len(store.for_project(pid).pr_checks) == 1 for pid in (alice, bob))
    assert store.for_project(showcase.id).pr_checks == {}
    assert post_webhook(client, "nobody/here").json() == {"unmapped": "nobody/here"}


# A valid body per write route, so the request gets past validation (422) to the access check.
BODIES = {
    "sources": {"data": {"kind": "transcript"}, "files": {"file": ("t.txt", TRANSCRIPT.encode(), "text/plain")}},
    "pr-check": {"json": {"diff": DIFF}},
    "ask": {"json": {"question": "why"}},
    "resolve": {"json": {"action": "reconsider"}},
    "reaffirm": {"json": {"note": ""}},
    "rename": {"json": {"name": "x"}},
    "merge": {"json": {"into": "T2"}},
    "move": {"json": {"decision_ids": ["1"], "into": "T2"}},
    "split": {"json": {"decision_ids": ["1"], "name": "y"}},
    "{project_id}": {"json": {"repo": "acme/demo"}},
}


def test_every_project_route_checks_access():
    """Review R1-08: any route under /api/projects/{project_id} answers 401 without a token and 403 for another
    account, before it touches the project; a new route that forgets the check turns this red."""
    client, store, _, _ = build()
    pid = me(client)
    routes = [(m, r.path) for r in router.routes for m in getattr(r, "methods", ())
              if r.path.startswith("/api/projects/{project_id}") and m in {"GET", "POST", "PATCH", "PUT", "DELETE"}]
    assert len(routes) >= 17  # sanity floor: the sweep really found the project routes
    for method, path in routes:
        url = path.replace("{project_id}", pid)
        for name in ("topic_id", "alert_id", "decision_id", "source_id"):
            url = url.replace("{" + name + "}", "1")
        kwargs = {} if method == "GET" else BODIES.get(path.rsplit("/", 1)[-1], {"json": {}})
        assert client.request(method, url, headers=BOB, **kwargs).status_code == 403, (method, path)
        assert client.request(method, url, **kwargs).status_code == 401, (method, path)


def test_an_expired_token_still_reads_the_showcase_but_cannot_write():
    """Review R1-06 / REQ-PROJECT-006: anyone reads the showcase; a bad token only matters where sign-in does."""
    bad = {"Authorization": "Bearer fake:"}
    for public_mode, refused in ((False, 401), (True, 403)):  # public mode's middleware refuses showcase writes first
        client, store, _, showcase = build(public_mode=public_mode)
        assert client.get(f"/api/projects/{showcase.id}/overview", headers=bad).status_code == 200
        response = upload(client, showcase.id, bad)
        problem(response, refused)
        assert store.for_project(showcase.id).sources == {}
        problem(client.get("/api/me", headers=bad), 401, "unauthenticated")
    assert "Sign in with Google" in response.json()["detail"]
