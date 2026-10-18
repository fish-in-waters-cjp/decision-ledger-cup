import json

import pytest
from fastapi.testclient import TestClient

from decision_ledger.api.app import create_app
from decision_ledger.api.webhook_auth import sign, verify_signature
from decision_ledger.bootstrap import ContainerFactory
from decision_ledger.domain.models import AlertKind, AlertStatus, CheckableRule, DecisionStatus, PullRequestDiff
from decision_ledger.services.parsers import content_hash
from tests.factories import alert, decision, topic
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


P = "/api/projects/P1"


def build(responses=None, secret=SECRET):
    """An app over a fake store holding project P1 (mapped to acme/app); returns P1's repository."""
    store, llm = FakeLedgerStore(), FakeLLM(responses or {})
    store.create_project("v2 專案", "acme/app")
    provider = ContainerFactory(store, llm, RecordingPublisher(), FakePullRequestSource(), webhook_secret=secret)
    return TestClient(create_app(provider)), store.for_project("P1"), llm


def assert_problem(response, status):
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == status and body["type"].startswith("/problems/") and body["title"]


# --- signature -----------------------------------------------------------


def test_verify_signature():
    body = b'{"a":1}'
    assert verify_signature(SECRET, body, sign(SECRET, body))
    assert not verify_signature(SECRET, body, sign("other", body))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(None, body, sign(SECRET, body))
    assert not verify_signature("", body, sign("", body))


def pr_payload(**extra):
    return {
        "action": "opened", "number": 87,
        "repository": {"full_name": "acme/app"},
        "pull_request": {"number": 87, "title": "報表", "user": {"login": "kevin"}, "head": {"sha": "abc"},
                         "diff_url": "https://github.com/acme/app/pull/87.diff"},
        **extra,
    }


def post_webhook(client, payload, event="pull_request", secret=SECRET, signature=None):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if signature is not False:
        headers["X-Hub-Signature-256"] = signature or sign(secret, body)
    return client.post("/webhook/github", content=body, headers=headers)


def checkable_repo(repo):
    repo.decisions["23"] = decision("23", statement="report-service 走 internal API",
                                    rule=CheckableRule(description="r", path_patterns=["src/report/*"]))


@pytest.mark.parametrize("signature", [False, "sha256=deadbeef"])
def test_webhook_with_bad_signature_is_401_and_never_calls_llm(signature):
    client, repo, llm = build()
    checkable_repo(repo)
    response = post_webhook(client, pr_payload(diff=DIFF), signature=signature)
    assert_problem(response, 401)
    assert llm.calls == [] and repo.pr_checks == {}


def test_webhook_without_configured_secret_is_401():
    client, _, llm = build(secret=None)
    assert_problem(post_webhook(client, pr_payload(diff=DIFF), secret="anything"), 401)
    assert llm.calls == []


def test_webhook_ping_and_other_events():
    client, _, llm = build()
    assert post_webhook(client, {"zen": "hi"}, event="ping").status_code == 200
    assert post_webhook(client, {"action": "created"}, event="issues").status_code == 202
    assert post_webhook(client, pr_payload(action="closed"), event="pull_request").status_code == 202
    assert llm.calls == []


VIOLATION_RESPONSE = {"DeviationJudgement": {"violations": [
    {"decision_id": "23", "file": "src/report/service.py", "line": 10, "snippet": "from app.db import db_client"}]}}


def test_webhook_pull_request_is_queued_and_checked_in_background():
    """DL-003 US-1.1: 202 with the queued key; the background check publishes once."""
    client, repo, llm = build(VIOLATION_RESPONSE)
    checkable_repo(repo)
    publisher = client.app.state.provider.publisher
    response = post_webhook(client, pr_payload(diff=DIFF))
    assert response.status_code == 202
    assert response.json() == {"queued": "acme/app#87@abc", "projects": 1}
    check = repo.pr_checks["acme/app#87@abc"]
    assert check.violated and check.pr_number == 87 and len(repo.alerts) == 1
    assert len(publisher.published) == 1 and len(llm.calls) == 1


def test_webhook_resend_is_202_and_publishes_once():
    """DL-003 US-1.2."""
    client, repo, llm = build(VIOLATION_RESPONSE)
    checkable_repo(repo)
    publisher = client.app.state.provider.publisher
    first = post_webhook(client, pr_payload(diff=DIFF))
    again = post_webhook(client, pr_payload(diff=DIFF))
    assert first.status_code == again.status_code == 202 and first.json() == again.json()
    assert len(publisher.published) == 1 and len(repo.alerts) == 1 and len(llm.calls) == 1


def test_webhook_plural_path_is_an_alias_with_the_same_signature_check():
    client, repo, llm = build(VIOLATION_RESPONSE)
    checkable_repo(repo)
    body = json.dumps(pr_payload(diff=DIFF)).encode()
    headers = {"X-GitHub-Event": "pull_request", "Content-Type": "application/json"}
    assert_problem(client.post("/webhooks/github", content=body, headers={**headers, "X-Hub-Signature-256": "sha256=00"}), 401)
    assert llm.calls == [] and repo.pr_checks == {}
    ok = client.post("/webhooks/github", content=body, headers={**headers, "X-Hub-Signature-256": sign(SECRET, body)})
    assert ok.status_code == 202 and "acme/app#87@abc" in repo.pr_checks


def test_webhook_background_failure_is_logged_with_key_and_not_retried(caplog):
    from decision_ledger.domain.errors import LLMTransportError

    client, repo, llm = build({"DeviationJudgement": [LLMTransportError("timeout"), LLMTransportError("timeout")]})
    checkable_repo(repo)
    with caplog.at_level("ERROR"):
        response = post_webhook(client, pr_payload(diff=DIFF))
    assert response.status_code == 202
    assert repo.pr_checks == {} and len(llm.calls) == 2
    assert any("acme/app#87@abc" in r.getMessage() for r in caplog.records)


def test_webhook_invalid_payload_is_400():
    client, _, _ = build()
    assert_problem(post_webhook(client, {"action": "opened"}), 400)


def test_webhook_without_diff_source_is_422():
    """No token and the anonymous download fails (a private repo answers 404): the diff is unavailable."""
    import urllib.error

    from decision_ledger.adapters.github import GitHubPullRequestSource

    def private_repo(method, url, headers, data=None, **kw):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    client, repo, _ = build()
    client.app.state.provider.for_project("P1").deviation.pr_source = GitHubPullRequestSource(http=private_repo)
    assert_problem(post_webhook(client, pr_payload()), 422)


# --- sources -------------------------------------------------------------


def test_import_source_returns_report():
    h = content_hash(TRANSCRIPT)
    client, repo, _ = build({f"extract:{h}": {"decisions": [{
        "topic": "v2 上線時程", "statement": "v2 在 10/15 上線", "evidence": [{"segment_index": 0, "quote": "那 v2 就定 10/15 上線"}]}]}})
    response = client.post(P + "/sources", data={"kind": "transcript", "title": "週會", "occurred_on": "9/1"},
                           files={"file": ("t.txt", TRANSCRIPT.encode(), "text/plain")})
    assert response.status_code == 200
    assert response.json()["created"] == ["1"] and repo.decisions["1"].decided_on == "9/1"


def test_import_bad_file_is_422_problem():
    client, _, _ = build()
    response = client.post(P + "/sources", data={"kind": "slack"}, files={"file": ("s.json", b"not json", "application/json")})
    assert_problem(response, 422)
    response = client.post(P + "/sources", data={"kind": "transcript"}, files={"file": ("t.txt", b"\xff\xfe", "text/plain")})
    assert_problem(response, 422)
    assert_problem(client.post(P + "/sources", data={"kind": "pdf"}, files={"file": ("t", b"x", "text/plain")}), 422)


def test_llm_failure_is_502_problem():
    client, _, _ = build({"ExtractionResult": [{"decisions": "bad"}, {"decisions": "bad"}]})
    response = client.post(P + "/sources", data={"kind": "transcript"}, files={"file": ("t.txt", TRANSCRIPT.encode(), "text/plain")})
    assert_problem(response, 502)
    assert response.json()["type"] == "/problems/llm-output-invalid"
    assert response.json()["title"] == "AI model returned an invalid response"


def test_llm_transport_failure_is_502_unavailable_and_writes_nothing():
    """DL-022 (B7): a failed model call is not reported as invalid output."""
    from decision_ledger.domain.errors import LLMTransportError

    client, repo, _ = build({"ExtractionResult": [LLMTransportError("exit 1"), LLMTransportError("exit 1")]})
    response = client.post(P + "/sources", data={"kind": "transcript"}, files={"file": ("t.txt", TRANSCRIPT.encode(), "text/plain")})
    assert_problem(response, 502)
    body = response.json()
    assert body["type"] == "/problems/llm-unavailable" and body["title"] == "AI model call failed"
    assert repo.sources == {} and repo.decisions == {} and repo.alerts == {}


def test_ask_transport_failure_is_502_unavailable():
    from decision_ledger.domain.errors import LLMTransportError

    client, repo, _ = build({"AnswerDraft": [LLMTransportError("timeout"), LLMTransportError("timeout")]})
    repo.decisions["1"] = decision("1", statement="v2 在 10/15 上線")
    response = client.post(P + "/ask", json={"question": "v2 什麼時候上線"})
    assert_problem(response, 502)
    assert response.json()["type"] == "/problems/llm-unavailable"


# --- PR check, alerts, decisions, ask, reads -----------------------------


def test_pr_check_endpoint():
    client, repo, _ = build()
    checkable_repo(repo)
    response = client.post(P + "/pr-check", json={"diff": DIFF, "title": "local"})
    assert response.status_code == 200 and "no violation found" in response.json()["check"]["comment"]
    assert client.get(P + "/pr-checks").json()[0]["title"] == "local"


def test_resolve_alert_flow_and_errors():
    client, repo, _ = build()
    repo.decisions["1"], repo.decisions["2"] = decision("1"), decision("2")
    repo.alerts["5"] = alert("5", AlertKind.SUPERSESSION, ["1", "2"], superseded_id="1", superseding_id="2")
    assert_problem(client.post(P + "/alerts/5/resolve", json={"action": "mark_exception"}), 422)
    ok = client.post(P + "/alerts/5/resolve", json={"action": "confirm_supersession", "note": "ok"})
    assert ok.status_code == 200 and ok.json()["status"] == "resolved"
    assert repo.decisions["1"].status is DecisionStatus.SUPERSEDED
    assert_problem(client.post(P + "/alerts/5/resolve", json={"action": "confirm_supersession"}), 409)
    assert_problem(client.post(P + "/alerts/404/resolve", json={"action": "reconsider"}), 404)
    assert_problem(client.post(P + "/alerts/5/resolve", json={"action": "nonsense"}), 422)


def test_reaffirm_endpoint():
    client, repo, _ = build()
    repo.decisions["1"] = decision("1", status=DecisionStatus.UNDER_REVIEW)
    assert client.post(P + "/decisions/1/reaffirm", json={"note": "n"}).json()["status"] == "active"
    assert_problem(client.post(P + "/decisions/1/reaffirm", json={}), 422)


def test_ask_endpoint():
    client, repo, _ = build({"AnswerDraft": {"answer": "因為 QA 不足", "cited_decision_ids": ["42"]}})
    repo.decisions["42"] = decision("42", statement="v2 上線延到 11/1")
    body = client.post(P + "/ask", json={"question": "為什麼 v2 延期？"}).json()
    assert body["found"] and body["citations"][0]["decision_id"] == "42"
    assert client.post(P + "/ask", json={"question": "午餐？"}).json()["text"] == "No related decision found"


def test_read_endpoints():
    client, repo, _ = build()
    repo.decisions["1"] = decision("1")
    repo.alerts["1"] = alert("1", AlertKind.ASSUMPTION, ["1"])
    overview = client.get(P + "/overview").json()
    assert overview["backend"] == "fake" and overview["health_counts"]["at_risk"] == 1
    assert client.get(P + "/decisions").json()[0]["health"] == "at_risk"
    assert len(client.get(P + "/alerts", params={"status": "open"}).json()) == 1
    assert_problem(client.get(P + "/sources/nope"), 404)


def test_decision_detail_endpoint_is_removed():
    client, repo, _ = build()
    repo.decisions["1"] = decision("1")
    for path in ("/api/decisions/1", P + "/decisions/1"):
        assert client.get(path).status_code in (404, 405), path


def test_web_index_is_served():
    client, _, _ = build()
    response = client.get("/")
    assert response.status_code == 200 and "Decision Ledger" in response.text


def test_static_assets_must_be_revalidated():
    """Without Cache-Control, browsers keep running stale JS after an upgrade."""
    client, _, _ = build()
    for path in ("/", "/styles.css", "/app.js", "/demo/manifest.json"):
        assert client.get(path).headers.get("cache-control") == "no-cache", path


@pytest.mark.parametrize("raw", [b"sha256=\xc3\xa9", b"sha256=\xe9"])
def test_non_ascii_signature_is_401_not_500(raw):
    """Review R1-1: a non-ASCII signature header must be rejected as 401, not crash with 500."""
    assert not verify_signature(SECRET, b"{}", raw.decode("latin-1"))
    client, _, llm = build()
    body = json.dumps(pr_payload(diff=DIFF)).encode()
    response = client.post("/webhook/github", content=body,
                           headers=[(b"X-GitHub-Event", b"pull_request"), (b"X-Hub-Signature-256", raw)])
    assert_problem(response, 401)
    assert llm.calls == []


def test_unexpected_error_is_a_500_problem(monkeypatch):
    """Verify 3/5 (Structured API errors): even unexpected failures answer in RFC 7807."""
    client, repo, _ = build()
    client = TestClient(client.app, raise_server_exceptions=False)

    def boom():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(client.app.state.provider.for_project("P1").query, "overview", boom)
    assert_problem(client.get(P + "/overview"), 500)


def test_overview_reports_the_projects_name():
    client, _, _ = build()
    assert client.get(P + "/overview").json()["project"] == "v2 專案"


# --- projects ------------------------------------------------------------


def test_list_and_create_projects():
    client, _, _ = build()
    created = client.post("/api/projects", json={"name": "行動 App", "repo": "acme/mobile"})
    assert created.status_code == 201 and created.json()["id"] == "P2"
    listed = client.get("/api/projects").json()
    assert [(p["name"], p["pending"]) for p in listed] == [("v2 專案", 0), ("行動 App", 0)]
    assert client.get("/api/projects/P2/overview").json()["decisions"] == []  # a new project is empty


# DL-032: a repo may be registered by several projects, so only blank or duplicate names are invalid.
@pytest.mark.parametrize("body", [{"name": "  "}, {"name": "v2 專案"}, {}])
def test_invalid_project_is_422_and_creates_nothing(body):
    client, _, _ = build()
    assert_problem(client.post("/api/projects", json=body), 422)
    assert len(client.get("/api/projects").json()) == 1


def test_unknown_project_is_404():
    client, _, _ = build()
    assert_problem(client.get("/api/projects/P9/overview"), 404)
    assert_problem(client.post("/api/projects/P9/pr-check", json={"diff": DIFF}), 404)


def test_another_projects_alert_or_decision_is_404_and_unchanged():
    client, repo, _ = build()
    client.post("/api/projects", json={"name": "行動 App"})
    repo.decisions["1"], repo.decisions["2"] = decision("1"), decision("2", status=DecisionStatus.UNDER_REVIEW)
    repo.alerts["5"] = alert("5", AlertKind.SUPERSESSION, ["1", "2"], superseded_id="1", superseding_id="2")
    assert_problem(client.post("/api/projects/P2/alerts/5/resolve", json={"action": "confirm_supersession"}), 404)
    assert_problem(client.post("/api/projects/P2/decisions/2/reaffirm", json={}), 404)
    assert repo.alerts["5"].status is AlertStatus.OPEN and repo.decisions["2"].status is DecisionStatus.UNDER_REVIEW


# --- webhook → project ---------------------------------------------------


def test_webhook_verifies_the_signature_before_looking_up_the_repo(monkeypatch):
    client, _, llm = build()
    looked_up = []
    projects = client.app.state.provider.projects
    monkeypatch.setattr(projects, "by_repo", lambda repo: looked_up.append(repo))
    assert_problem(post_webhook(client, pr_payload(diff=DIFF), signature="sha256=deadbeef"), 401)
    assert looked_up == [] and llm.calls == []


def test_webhook_for_an_unmapped_repo_is_202_without_llm():
    client, repo, llm = build()
    checkable_repo(repo)
    payload = pr_payload(diff=DIFF, repository={"full_name": "someone/else"})
    response = post_webhook(client, payload)
    assert response.status_code == 202 and response.json() == {"unmapped": "someone/else"}
    assert llm.calls == [] and repo.pr_checks == {} and repo.alerts == {}


# --- topics --------------------------------------------------------------


def topic_client():
    client, repo, llm = build()
    repo.topics.update({"T1": topic("T1", "DB 選擇"), "T2": topic("T2", "資料庫選型"), "T3": topic("T3", "舊名", merged_into="T1")})
    repo.decisions["1"] = decision("1", topic_id="T1")
    repo.decisions["2"] = decision("2", topic_id="T2", status=DecisionStatus.UNDER_REVIEW)
    return client, repo, llm


def test_topic_list_detail_rename_and_merge():
    client, repo, _ = topic_client()
    assert [c["topic"]["id"] for c in client.get(P + "/topics").json()] == ["T2", "T1"]  # yellow (under review) first
    assert [n["decision"]["id"] for n in client.get(P + "/topics/T1").json()["nodes"]] == ["1"]
    assert client.get(P + "/topics/T3").json()["merged_into"]["id"] == "T1"
    assert client.post(P + "/topics/T1/rename", json={"name": "資料庫"}).json()["name"] == "資料庫"
    merged = client.post(P + "/topics/T2/merge", json={"into": "T1"})
    assert merged.status_code == 200 and merged.json()["id"] == "T1"
    assert repo.decisions["2"].topic_id == "T1" and repo.decisions["2"].status is DecisionStatus.UNDER_REVIEW
    assert repo.alerts == {}
    assert [c["topic"]["id"] for c in client.get(P + "/topics").json()] == ["T1"]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/topics/T1/merge", {"into": "T1"}),
        ("/topics/T1/merge", {"into": "T3"}),
        ("/topics/T3/merge", {"into": "T2"}),
        ("/topics/T1/merge", {"into": "T404"}),
        ("/topics/T1/rename", {"name": " "}),
        ("/topics/T1/rename", {"name": "資料庫選型"}),
    ],
)
def test_invalid_topic_operation_is_422_and_changes_nothing(path, body):
    client, repo, _ = topic_client()
    before = (dict(repo.topics), dict(repo.decisions), dict(repo.alerts))
    assert_problem(client.post(P + path, json=body), 422)
    assert (dict(repo.topics), dict(repo.decisions), dict(repo.alerts)) == before


def test_unknown_topic_is_404():
    client, _, _ = topic_client()
    assert_problem(client.get(P + "/topics/T404"), 404)
    assert_problem(client.post(P + "/topics/T404/rename", json={"name": "x"}), 404)


def test_move_and_split_decisions():
    client, repo, _ = topic_client()
    repo.decisions["3"] = decision("3", topic_id="T1", statement="每日備份")
    moved = client.post(P + "/topics/T1/move", json={"decision_ids": ["3"], "into": "T2"})
    assert moved.status_code == 200 and moved.json()["id"] == "T2"
    assert repo.decisions["3"].topic_id == "T2" and repo.decisions["3"].status is DecisionStatus.ACTIVE
    split = client.post(P + "/topics/T2/split", json={"decision_ids": ["3"], "name": "備份策略"})
    assert split.status_code == 200 and split.json()["name"] == "備份策略"
    assert repo.decisions["3"].topic_id == split.json()["id"] and repo.alerts == {}


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/topics/T1/move", {"decision_ids": ["1"], "into": "T1"}),
        ("/topics/T1/move", {"decision_ids": ["1"], "into": "T3"}),
        ("/topics/T1/move", {"decision_ids": ["1"], "into": "T404"}),
        ("/topics/T1/move", {"decision_ids": [], "into": "T2"}),
        ("/topics/T1/split", {"decision_ids": ["1"], "name": " "}),
        ("/topics/T1/split", {"decision_ids": ["1"], "name": "資料庫選型"}),
        ("/topics/T1/split", {"decision_ids": ["1"], "name": "全部選取"}),
    ],
)
def test_invalid_move_or_split_is_422_and_changes_nothing(path, body):
    client, repo, _ = topic_client()
    before = (dict(repo.topics), dict(repo.decisions))
    assert_problem(client.post(P + path, json=body), 422)
    assert (dict(repo.topics), dict(repo.decisions)) == before


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/topics/T404/move", {"decision_ids": ["1"], "into": "T2"}),
        ("/topics/T1/move", {"decision_ids": ["999"], "into": "T2"}),
        ("/topics/T1/move", {"decision_ids": ["2"], "into": "T2"}),
        ("/topics/T1/split", {"decision_ids": ["2"], "name": "新主題"}),
    ],
)
def test_unknown_topic_or_foreign_decision_is_404(path, body):
    client, repo, _ = topic_client()
    before = (dict(repo.topics), dict(repo.decisions))
    assert_problem(client.post(P + path, json=body), 404)
    assert (dict(repo.topics), dict(repo.decisions)) == before


def test_pr_check_transport_failure_is_502_unavailable():
    """DL-022 review F-5: the deviation check shares call_structured, so it reports the same problem type."""
    from decision_ledger.domain.errors import LLMTransportError

    client, repo, _ = build({"DeviationJudgement": [LLMTransportError("timeout"), LLMTransportError("timeout")]})
    checkable_repo(repo)
    response = client.post(P + "/pr-check", json={"diff": DIFF, "title": "local"})
    assert_problem(response, 502)
    assert response.json()["type"] == "/problems/llm-unavailable" and repo.pr_checks == {}



def _raw_webhook_request(app, body, event="pull_request"):
    from starlette.requests import Request as StarletteRequest

    headers = [(b"x-github-event", event.encode()), (b"x-hub-signature-256", sign(SECRET, body).encode()),
               (b"content-type", b"application/json")]
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/webhook/github", "headers": headers, "app": app,
             "query_string": b""}
    return StarletteRequest(scope, receive)


def test_webhook_answers_before_the_llm_runs():
    """DL-003 review R1-T1: the handler returns 202 with the check only queued, not run."""
    import asyncio

    from fastapi import BackgroundTasks

    from decision_ledger.api.routes import github_webhook

    client, repo, llm = build(VIOLATION_RESPONSE)
    checkable_repo(repo)
    body = json.dumps(pr_payload(diff=DIFF)).encode()
    background = BackgroundTasks()
    request = _raw_webhook_request(client.app, body)
    response = asyncio.run(github_webhook(request, background, "pull_request", sign(SECRET, body)))
    assert response.status_code == 202 and llm.calls == [] and repo.pr_checks == {}
    assert len(background.tasks) == 1
    asyncio.run(background())
    assert len(llm.calls) == 1 and "acme/app#87@abc" in repo.pr_checks


def test_slow_diff_download_is_422_within_the_deadline(monkeypatch):
    """DL-003 review R1-E1: a total deadline, not a per-read timeout, bounds the synchronous diff download."""
    import time

    from decision_ledger.api import routes

    client, repo, llm = build()
    checkable_repo(repo)
    monkeypatch.setattr(routes, "WEBHOOK_DIFF_DEADLINE", 0.05)
    deviation = client.app.state.provider.for_project("P1").deviation
    monkeypatch.setattr(deviation, "resolve_webhook", lambda event: time.sleep(0.5))
    started = time.monotonic()
    response = post_webhook(client, pr_payload(diff=DIFF))
    assert_problem(response, 422)
    assert time.monotonic() - started < 0.45 and llm.calls == []


def test_webhook_deadline_is_inside_githubs_ten_seconds():
    from decision_ledger.api.routes import WEBHOOK_DIFF_DEADLINE

    assert WEBHOOK_DIFF_DEADLINE < 10



# --- DL-017: replay the demo PRs without GitHub ---

REPLAY_87 = PullRequestDiff(repo="acme/app", pr_number=87, head_sha="r87", title="violating", author="kevin", diff=DIFF)
REPLAY_88 = PullRequestDiff(repo="acme/app", pr_number=88, head_sha="r88", title="compliant", author="grace",
                            diff=DIFF.replace("from app.db import db_client", "# timezone fix"))


def replay_client(responses):
    store, llm = FakeLedgerStore(), FakeLLM(responses)
    store.create_project("v2 專案", "acme/app")
    source = FakePullRequestSource()
    provider = ContainerFactory(store, llm, RecordingPublisher(), source, webhook_secret=SECRET,
                                replays={87: REPLAY_87, 88: REPLAY_88})
    return TestClient(create_app(provider)), store.for_project("P1"), llm, source


def test_replay_violating_and_compliant_prs_without_github():
    responses = {"deviation:acme/app#87@r87": VIOLATION_RESPONSE["DeviationJudgement"],
                 "deviation:acme/app#88@r88": {"violations": []}}
    client, repo, llm, source = replay_client(responses)
    checkable_repo(repo)
    violating = client.post(P + "/pr-check", json={"fixture_pr": 87})
    assert violating.status_code == 200
    body = violating.json()
    assert body["check"]["violated"] and body["check"]["pr_number"] == 87 and len(body["alerts"]) == 1
    assert body["alerts"][0]["evidence"] and "report-service" in body["check"]["comment"]
    compliant = client.post(P + "/pr-check", json={"fixture_pr": 88}).json()
    assert not compliant["check"]["violated"] and compliant["check"]["pr_number"] == 88
    assert source.calls == [] and client.app.state.provider.publisher.published[0][1] == body["check"]["comment"]


def test_replay_again_returns_the_stored_result():
    client, repo, llm, _ = replay_client({"deviation:acme/app#87@r87": VIOLATION_RESPONSE["DeviationJudgement"]})
    checkable_repo(repo)
    client.post(P + "/pr-check", json={"fixture_pr": 87})
    again = client.post(P + "/pr-check", json={"fixture_pr": 87}).json()
    assert again["reused"] and len(llm.calls) == 1 and len(repo.alerts) == 1


@pytest.mark.parametrize("body", [{"fixture_pr": 87, "diff": DIFF}, {}, {"fixture_pr": 99}, {"title": "x"}])
def test_pr_check_needs_exactly_one_of_diff_or_fixture(body):
    client, _, llm, _ = replay_client({})
    assert_problem(client.post(P + "/pr-check", json=body), 422)
    assert llm.calls == []



def test_replay_before_any_checkable_decision_is_409_and_writes_nothing():
    """DL-017 plan FLAWS: an empty project must not store a 'passed' result under the replay key."""
    client, repo, llm, _ = replay_client({"deviation:acme/app#87@r87": VIOLATION_RESPONSE["DeviationJudgement"]})
    early = client.post(P + "/pr-check", json={"fixture_pr": 87})
    assert_problem(early, 409)
    assert early.json()["type"] == "/problems/replay-not-ready"
    assert repo.pr_checks == {} and llm.calls == []
    checkable_repo(repo)
    later = client.post(P + "/pr-check", json={"fixture_pr": 87}).json()
    assert later["check"]["violated"] and not later["reused"]


def test_replay_of_a_missing_fixture_is_404():
    store, llm = FakeLedgerStore(), FakeLLM({})
    store.create_project("v2 專案", "acme/app")
    provider = ContainerFactory(store, llm, RecordingPublisher(), FakePullRequestSource(), replays={87: REPLAY_87})
    client = TestClient(create_app(provider))
    checkable_repo(store.for_project("P1"))
    assert_problem(client.post(P + "/pr-check", json={"fixture_pr": 88}), 404)



# --- DL-021: daily LLM limits ---

def test_llm_call_over_the_daily_limit_is_429_and_writes_nothing():
    from tests.fakes import FakeUsageCounter

    store, llm = FakeLedgerStore(), FakeLLM({})
    store.create_project("v2 專案", "acme/app")
    provider = ContainerFactory(store, llm, RecordingPublisher(), FakePullRequestSource(), webhook_secret=SECRET,
                                usage=FakeUsageCounter(), sandbox_daily_llm_limit=0, site_daily_llm_limit=100)
    client = TestClient(create_app(provider))
    response = client.post(P + "/sources", data={"kind": "transcript"}, files={"file": ("t.txt", TRANSCRIPT.encode(), "text/plain")})
    assert_problem(response, 429)
    assert response.json()["type"] == "/problems/rate-limited"
    assert llm.calls == [] and store.for_project("P1").sources == {}



def test_site_wide_daily_limit_is_429_for_any_project():
    """DL-021: the site-wide cap refuses a project that is still within its own limit."""
    from tests.fakes import FakeUsageCounter

    store, llm = FakeLedgerStore(), FakeLLM({})
    store.create_project("v2 專案", "acme/app")
    counter = FakeUsageCounter()
    provider = ContainerFactory(store, llm, RecordingPublisher(), FakePullRequestSource(), webhook_secret=SECRET,
                                usage=counter, sandbox_daily_llm_limit=100, site_daily_llm_limit=0)
    client = TestClient(create_app(provider))
    response = client.post(P + "/sources", data={"kind": "transcript"}, files={"file": ("t.txt", TRANSCRIPT.encode(), "text/plain")})
    assert_problem(response, 429)
    assert response.json()["type"] == "/problems/rate-limited" and llm.calls == []
    assert counter.get("sandbox", next(k for (scope, k) in counter.counts if scope == "sandbox")) == 1
    assert store.for_project("P1").sources == {}


def test_problem_titles_are_english():
    """REQ-LEDGER-PLATFORM-027 AC2: judge-facing problem+json titles are English."""
    client, _, _ = build({"ExtractionResult": [{"decisions": "bad"}, {"decisions": "bad"}]})
    llm_failure = client.post(P + "/sources", data={"kind": "transcript"}, files={"file": ("t.txt", TRANSCRIPT.encode(), "text/plain")})
    assert llm_failure.status_code == 502 and llm_failure.json()["title"] == "AI model returned an invalid response"
    invalid = client.post(P + "/alerts/5/resolve", json={"action": "nonsense"})
    assert invalid.status_code == 422 and invalid.json()["title"] == "Invalid request fields"
