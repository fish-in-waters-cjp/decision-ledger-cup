"""DL-020: health endpoint that never calls the LLM or writes the store."""

from fastapi.testclient import TestClient

from decision_ledger.api.app import create_app
from decision_ledger.bootstrap import ContainerFactory
from decision_ledger.services.container import RuntimeInfo, ShowcaseState
from tests.fakes import FakeLedgerStore, FakeLLM, FakePullRequestSource, RecordingPublisher

FIELDS = {"status", "version", "llm_backend", "model", "store", "public_mode", "recorded",
          "showcase", "showcase_project", "showcase_recorded", "instance_id", "api_base"}


def build(runtime=None):
    store, llm = FakeLedgerStore(), FakeLLM({})
    store.create_project("v2", None)
    provider = ContainerFactory(store, llm, RecordingPublisher(), FakePullRequestSource())
    return TestClient(create_app(provider, runtime=runtime)), store, llm


def test_healthz_reports_runtime_without_touching_llm_or_store():
    state = ShowcaseState("ready")
    runtime = RuntimeInfo(version="9.9", llm_backend="gemini", model="gemini-3.5-flash", store="sqlite",
                          public_mode=True, recorded=False, showcase_project="P1", showcase_recorded=True,
                          showcase_state=state)
    client, store, llm = build(runtime)
    before = store.list_projects()
    for path in ("/healthz", "/api/health"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
        body = r.json()
        assert set(body) == FIELDS
        assert body["status"] == "ok" and body["llm_backend"] == "gemini" and body["model"] == "gemini-3.5-flash"
        assert body["public_mode"] is True and body["recorded"] is False and body["showcase"] == "ready"
        assert body["showcase_project"] == "P1" and body["showcase_recorded"] is True and body["version"] == "9.9"
    assert llm.calls == [] and store.list_projects() == before


def test_instance_id_is_random_per_runtime_and_stable_within_one():
    a, b = RuntimeInfo(), RuntimeInfo()
    assert a.instance_id != b.instance_id and len(a.instance_id) == 16
    client, _, _ = build(a)
    assert client.get("/api/health").json()["instance_id"] == client.get("/api/health").json()["instance_id"] == a.instance_id


def test_healthz_reflects_showcase_state_changes():
    state = ShowcaseState()
    client, _, _ = build(RuntimeInfo(showcase_state=state))
    assert client.get("/healthz").json()["showcase"] == "absent"
    state.status = "failed"
    assert client.get("/healthz").json()["showcase"] == "failed"


def test_healthz_defaults_when_no_runtime_given():
    client, _, _ = build()
    body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["llm_backend"] == "unknown" and body["recorded"] is False
    assert body["api_base"] is None


def test_health_reports_api_base_for_long_requests():
    client, _, _ = build(RuntimeInfo(api_base="https://decision-ledger-cpgrpijo3a-de.a.run.app"))
    assert client.get("/api/health").json()["api_base"] == "https://decision-ledger-cpgrpijo3a-de.a.run.app"


def test_healthz_is_not_shadowed_by_the_web_ui():
    client, _, _ = build()
    assert client.get("/").headers["content-type"].startswith("text/html")
    assert client.get("/healthz").headers["content-type"].startswith("application/json")


def test_web_ui_has_recorded_banner_hooks():
    from decision_ledger.api.app import STATIC_DIR

    assert 'id="recorded"' in (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "runtime.js").read_text(encoding="utf-8")
    from tests.ui_text import shows

    assert shows(STATIC_DIR / "runtime.js", "Recorded run") and "showcase_recorded" in js  # DL-031: literal or en.js key
    # /healthz never reaches the app behind Google's front end, so the UI must use the alias.
    assert "fetch('/api/health')" in js and "fetch('/healthz')" not in js
