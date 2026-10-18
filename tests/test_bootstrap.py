import builtins
import sqlite3
from types import SimpleNamespace

import pytest

import decision_ledger
from decision_ledger import bootstrap
from decision_ledger.adapters.github import DryRunCommentPublisher, GitHubCommentPublisher
from decision_ledger.adapters.llm.claude_code import ClaudeCodeLLM
from decision_ledger.adapters.llm.fake import FakeReplayLLM
from decision_ledger.bootstrap import Settings, build_llm, build_provider, ensure_default_project
from decision_ledger.domain.errors import NotFound
from tests.fakes import FakeLedgerStore


def default_container(settings):
    provider, project = build_provider(settings)
    return provider.for_project(project.id)


def test_settings_defaults_are_credential_free():
    s = Settings.from_env({})
    assert s.llm_backend == "fake" and s.host == "127.0.0.1" and s.github_dry_run is True
    assert s.webhook_secret is None and s.llm_timeout == 60.0


def test_settings_read_environment():
    s = Settings.from_env({
        "DL_LLM_BACKEND": "claude_code", "DL_DB_PATH": "/tmp/x.db", "DL_WEBHOOK_SECRET": "w",
        "DL_LLM_TIMEOUT": "5", "DL_DIFF_CHAR_LIMIT": "100", "DL_GITHUB_TOKEN": "t", "DL_GITHUB_DRY_RUN": "false",
    })
    assert (s.llm_backend, s.db_path, s.webhook_secret, s.llm_timeout, s.diff_char_limit) == ("claude_code", "/tmp/x.db", "w", 5.0, 100)
    assert s.github_token == "t" and s.github_dry_run is False


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError):
        build_llm(Settings.from_env({"DL_LLM_BACKEND": "gpt"}))


def test_backend_selection_is_configuration_only():
    assert isinstance(build_llm(Settings.from_env({})), FakeReplayLLM)
    assert isinstance(build_llm(Settings.from_env({"DL_LLM_BACKEND": "claude_code"})), ClaudeCodeLLM)


def test_fake_backend_works_without_gemini_sdk(monkeypatch):
    real_import = builtins.__import__

    def no_google(name, *args, **kwargs):
        if name.startswith("google"):
            raise ImportError("google-genai not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_google)
    container = default_container(Settings.from_env({"DL_DB_PATH": ":memory:"}))
    assert container.query.overview().backend == "fake"
    with pytest.raises(RuntimeError, match="gemini"):
        build_llm(Settings.from_env({"DL_LLM_BACKEND": "gemini"}))


def test_publisher_is_always_dry_run():
    """DL-004: no GitHub write API ever, whatever the token and dry-run settings."""
    for env in ({}, {"DL_GITHUB_TOKEN": "t"}, {"DL_GITHUB_TOKEN": "t", "DL_GITHUB_DRY_RUN": "false"}):
        publisher = bootstrap.build_publisher(Settings.from_env(env))
        assert isinstance(publisher, DryRunCommentPublisher) and not isinstance(publisher, GitHubCommentPublisher)


def test_public_url_reaches_the_deviation_service():
    assert Settings.from_env({}).public_url is None and Settings.from_env({"DL_PUBLIC_URL": " "}).public_url is None
    settings = Settings.from_env({"DL_DB_PATH": ":memory:", "DL_PUBLIC_URL": "https://ledger.example.app/"})
    assert settings.public_url == "https://ledger.example.app"
    provider, project = build_provider(settings)
    assert provider.for_project(project.id).deviation.public_url == "https://ledger.example.app"


def test_provider_carries_the_global_webhook_secret_and_diff_limit():
    provider, project = build_provider(Settings.from_env({"DL_DB_PATH": ":memory:", "DL_WEBHOOK_SECRET": "w", "DL_DIFF_CHAR_LIMIT": "77"}))
    assert provider.webhook_secret == "w"
    assert provider.for_project(project.id).deviation.diff_char_limit == 77


def test_factory_caches_one_container_per_project_and_rejects_unknown():
    provider, project = build_provider(Settings.from_env({"DL_DB_PATH": ":memory:"}))
    other = provider.projects.create("行動 App")
    a, b = provider.for_project(project.id), provider.for_project(other.id)
    assert a is provider.for_project(project.id) and a is not b
    assert a.ingestion.repo.project_id == project.id and b.topics.repo.project_id == other.id
    # DL-021: each project wraps the shared model client in its own usage limiter.
    assert a.ingestion.llm.inner is b.ingestion.llm.inner and a.deviation.publisher is b.deviation.publisher
    assert a.ingestion.llm is not b.ingestion.llm and a.ingestion.llm is a.qa.llm
    with pytest.raises(NotFound):
        provider.for_project("P404")


def test_default_project_is_created_once():
    store = FakeLedgerStore()
    first = ensure_default_project(store, "本機 Ledger")
    store.create_project("另一個", None)
    assert ensure_default_project(store, "改了名字") == first
    assert [p.name for p in store.list_projects()] == ["本機 Ledger", "另一個"]


def old_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE sources (id TEXT PRIMARY KEY); PRAGMA user_version = 2;")
    conn.close()
    return path


@pytest.mark.parametrize("command", [["serve"], ["demo", "--db", "{db}"]])
def test_main_refuses_an_old_database_with_exit_2(monkeypatch, capsys, tmp_path, command):
    db = old_database(tmp_path)
    before = db.read_bytes()
    monkeypatch.setenv("DL_DB_PATH", str(db))
    monkeypatch.setattr(bootstrap, "uvicorn", SimpleNamespace(run=lambda *a, **k: pytest.fail("must not serve")))
    with pytest.raises(SystemExit) as exit_info:
        decision_ledger.main([part.format(db=db) for part in command])
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert str(db) in err and "Delete the database file" in err
    assert db.read_bytes() == before


def test_main_serve_binds_localhost_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(bootstrap, "uvicorn", SimpleNamespace(run=lambda app, host, port: calls.append((host, port))))
    monkeypatch.setenv("DL_DB_PATH", ":memory:")
    decision_ledger.main(["serve"])
    assert calls == [("127.0.0.1", 8000)]
    decision_ledger.main(["serve", "--host", "0.0.0.0", "--port", "9000"])
    assert calls[-1] == ("0.0.0.0", 9000)


def test_main_demo_prints_summary(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "run_demo", lambda container: {"decisions": 3, "conflict": 1} if container.topics else {})
    decision_ledger.main(["demo"])
    out = capsys.readouterr().out
    assert "decisions" in out and "conflict" in out


def test_project_name_is_configurable():
    assert Settings.from_env({}).project_name == "Local Ledger"
    c = default_container(Settings.from_env({"DL_DB_PATH": ":memory:", "DL_PROJECT_NAME": "Northwind · v2"}))
    assert c.query.overview().project == "Northwind · v2"


def test_default_project_repo_comes_from_settings():
    """Review R1-3: a deployment that only uses the default project can still receive webhooks."""
    settings = Settings.from_env({"DL_DB_PATH": ":memory:", "DL_PROJECT_REPO": " northwind/v2 "})
    provider, project = build_provider(settings)
    assert project.repo == "northwind/v2"
    assert provider.projects.by_repo("northwind/v2") == [project]
    assert Settings.from_env({}).project_repo is None


# --- DL-020 runtime info ------------------------------------------------------


def test_recorded_mode_follows_env_or_fake_backend():
    from decision_ledger.bootstrap import build_runtime

    assert build_runtime(Settings.from_env({})).recorded is True  # fake backend
    gem = {"DL_LLM_BACKEND": "gemini", "DL_GEMINI_MODEL": "gemini-3.5-flash"}
    assert build_runtime(Settings.from_env(gem)).recorded is False
    assert build_runtime(Settings.from_env({**gem, "DL_RECORDED_MODE": "1"})).recorded is True
    info = build_runtime(Settings.from_env(gem), showcase_project="P1")
    assert (info.llm_backend, info.model, info.store, info.showcase_project) == ("gemini", "gemini-3.5-flash", "sqlite", "P1")
    assert info.version == decision_ledger.__version__


# --- DL-006 deployment settings -------------------------------------------------


def test_deployment_settings_parse_env():
    s = Settings.from_env({"DL_CORS_ORIGINS": " https://a.web.app , https://b.web.app ,", "DL_PUBLIC_URL": "https://a.web.app",
                           "DL_SANDBOX_SALT": "salt"})
    assert s.cors_origins == ("https://a.web.app", "https://b.web.app")
    assert s.public_url == "https://a.web.app" and s.sandbox_salt == "salt"
    empty = Settings.from_env({})
    assert empty.cors_origins == () and empty.public_url is None and empty.sandbox_salt is None


# --- DL-018 public mode wiring -------------------------------------------------


def test_public_mode_runtime_and_forget(tmp_path):
    from decision_ledger.bootstrap import build_runtime

    s = Settings.from_env({"DL_PUBLIC_MODE": "1", "DL_DB_PATH": str(tmp_path / "p.db")})
    assert s.public_mode is True and Settings.from_env({}).public_mode is False
    provider, project = build_provider(s)
    info = build_runtime(s, showcase_project=project.id)
    assert info.public_mode is True and info.showcase_project == project.id
    assert provider.sandboxes.showcase_id == project.id
    first = provider.for_project(project.id)
    provider.forget(project.id)
    assert provider.for_project(project.id) is not first
# --- DL-002: pinned Gemini model and retirement guard ---

def test_default_gemini_model_is_pinned_and_allowed():
    s = Settings.from_env({})
    assert s.gemini_model == "gemini-3.5-flash"
    bootstrap.check_gemini_model(s.gemini_model)  # does not raise


@pytest.mark.parametrize("model", ["gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.8-flash"])
def test_allow_list_models_pass(model):
    bootstrap.check_gemini_model(model)


def test_retiring_model_is_refused_with_its_retirement_date():
    with pytest.raises(bootstrap.ModelNotAllowed) as info:
        bootstrap.check_gemini_model("gemini-2.5-flash")
    assert "gemini-2.5-flash" in str(info.value) and "2026-10-20" in str(info.value)


def test_unknown_model_is_refused_and_lists_the_allow_list():
    with pytest.raises(bootstrap.ModelNotAllowed) as info:
        bootstrap.check_gemini_model("gemini-latest")
    assert "gemini-latest" in str(info.value) and "gemini-3.5-flash" in str(info.value)


def test_model_retiring_on_the_cutoff_day_is_refused(monkeypatch):
    info = bootstrap.GeminiModelInfo(retires_on=bootstrap.JUDGING_CUTOFF)
    monkeypatch.setitem(bootstrap.GEMINI_MODEL_RETIREMENTS, "gemini-edge", info)
    with pytest.raises(bootstrap.ModelNotAllowed):
        bootstrap.check_gemini_model("gemini-edge")


def test_gemini_location_precedence():
    assert Settings.from_env({}).gcp_location == "global"
    assert Settings.from_env({"GOOGLE_CLOUD_LOCATION": "us-central1"}).gcp_location == "us-central1"
    both = {"GOOGLE_CLOUD_LOCATION": "us-central1", "DL_GEMINI_LOCATION": "global"}
    assert Settings.from_env(both).gcp_location == "global"


def test_build_llm_passes_model_vertex_and_location(monkeypatch):
    import decision_ledger.adapters.llm.gemini as gemini_module

    seen = {}

    class Recorder:
        name = "gemini"

        def __init__(self, model, **kwargs):
            seen.update(model=model, **kwargs)

    monkeypatch.setattr(gemini_module, "GeminiLLM", Recorder)
    build_llm(Settings.from_env({"DL_LLM_BACKEND": "gemini", "DL_GEMINI_VERTEXAI": "true", "DL_GEMINI_LOCATION": "global",
                                 "DL_GEMINI_MODEL": "gemini-3.7-flash"}))
    assert seen["model"] == "gemini-3.7-flash" and seen["vertexai"] is True and seen["location"] == "global"


def test_serve_refuses_retiring_model_before_building_anything(monkeypatch):
    monkeypatch.setattr(bootstrap, "build_provider", lambda s: pytest.fail("must not build a provider"))
    with pytest.raises(bootstrap.ModelNotAllowed):
        bootstrap.serve(Settings.from_env({"DL_LLM_BACKEND": "gemini", "DL_GEMINI_MODEL": "gemini-2.5-flash"}))


def test_main_serve_exits_2_on_retiring_model(monkeypatch, capsys):
    monkeypatch.setenv("DL_LLM_BACKEND", "gemini")
    monkeypatch.setenv("DL_GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(bootstrap, "uvicorn", SimpleNamespace(run=lambda *a, **k: pytest.fail("must not serve")))
    with pytest.raises(SystemExit) as exit_info:
        decision_ledger.main(["serve"])
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "gemini-2.5-flash" in err and "2026-10-20" in err


def test_demo_ignores_gemini_settings(monkeypatch, capsys):
    monkeypatch.setenv("DL_LLM_BACKEND", "gemini")
    monkeypatch.setenv("DL_GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(bootstrap, "run_demo", lambda container: {"decisions": 1, "backend": container.query.overview().backend == "fake"})
    decision_ledger.main(["demo"])
    assert "backend: True" in capsys.readouterr().out



def test_blank_gemini_model_falls_back_to_default():
    """DL-002 review R3."""
    assert Settings.from_env({"DL_GEMINI_MODEL": "  "}).gemini_model == "gemini-3.5-flash"
    assert Settings.from_env({"DL_GEMINI_MODEL": " gemini-3.7-flash "}).gemini_model == "gemini-3.7-flash"



def test_demo_pull_requests_are_loaded_for_replay():
    """DL-017: the replayable PRs come from the demo manifest."""
    prs = bootstrap.load_demo_pull_requests()
    assert set(prs) == {87, 88}
    assert prs[87].pr_number == 87 and prs[87].head_sha and prs[87].diff.startswith("diff --git")
    provider, project = build_provider(Settings.from_env({"DL_DB_PATH": ":memory:"}))
    assert set(provider.for_project(project.id).deviation.replays) == {87, 88}



def test_missing_demo_manifest_means_no_replays(tmp_path):
    assert bootstrap.load_demo_pull_requests(tmp_path) == {}



def test_demo_source_hashes_cover_the_sources_before_the_pr_step():
    from decision_ledger.services.parsers import content_hash

    hashes = bootstrap.load_demo_source_hashes()
    raw = (bootstrap.DEMO_DIR / "01-kickoff-0908.txt").read_text(encoding="utf-8")
    assert content_hash(raw) in hashes and len(hashes) == 3
    assert bootstrap.load_demo_source_hashes(bootstrap.DEMO_DIR / "missing") == ()



def test_incomplete_demo_fixtures_disable_replay_instead_of_crashing(tmp_path):
    """DL-017 review: a manifest that lists a missing file must not stop the service from starting."""
    (tmp_path / "manifest.json").write_text(
        '{"steps": [{"type": "source", "file": "gone.txt", "kind": "transcript", "title": "t"},'
        ' {"type": "prs", "pull_requests": [{"file": "gone.diff", "pr_number": 87}]}]}', encoding="utf-8")
    assert bootstrap.load_demo_pull_requests(tmp_path) == {}
    assert bootstrap.load_demo_source_hashes(tmp_path) == ()



def test_daily_llm_limits_come_from_the_environment_and_wrap_the_llm():
    from decision_ledger.services.usage import LimitedLLM

    defaults = Settings.from_env({})
    assert (defaults.sandbox_daily_llm_limit, defaults.site_daily_llm_limit) == (60, 1500)
    s = Settings.from_env({"DL_DB_PATH": ":memory:", "DL_SANDBOX_DAILY_LLM_LIMIT": "5", "DL_SITE_DAILY_LLM_LIMIT": "9"})
    provider, project = build_provider(s)
    llm = provider.for_project(project.id).ingestion.llm
    assert isinstance(llm, LimitedLLM) and (llm.sandbox_limit, llm.site_limit) == (5, 9) and llm.project_id == project.id


def test_showcase_rebuild_counts_toward_the_site_limit_only():
    """Decision: the showcase rebuild counts toward the site-wide daily limit, never a sandbox limit."""
    from decision_ledger.services.container import ShowcaseState
    from decision_ledger.services.usage import usage_day

    s = Settings.from_env({"DL_DB_PATH": ":memory:"})
    provider, project = build_provider(s)
    state = ShowcaseState()
    bootstrap.rebuild_showcase(provider, project.id, state)
    day = usage_day()
    assert state.status == "ready"
    assert provider.usage.get("site", day) > 0
    assert provider.usage.get("sandbox", f"{project.id}:{day}") == 0


# --- DL-030 direct API base for long LLM requests --------------------------------


def test_api_base_setting_reaches_runtime():
    from decision_ledger.bootstrap import build_runtime

    assert Settings.from_env({}).api_base is None and build_runtime(Settings.from_env({})).api_base is None
    s = Settings.from_env({"DL_API_BASE": " https://decision-ledger-cpgrpijo3a-de.a.run.app/ "})
    assert s.api_base == "https://decision-ledger-cpgrpijo3a-de.a.run.app"
    assert build_runtime(s).api_base == s.api_base


def test_record_path_wraps_the_backend(tmp_path):
    from decision_ledger.adapters.llm.recording import RecordingLLM

    wrapped = build_llm(Settings.from_env({"DL_LLM_RECORD_PATH": str(tmp_path / "rec.json")}))
    assert isinstance(wrapped, RecordingLLM) and isinstance(wrapped.inner, FakeReplayLLM)
    assert wrapped.name == "fake" and wrapped.path == tmp_path / "rec.json"
    assert isinstance(build_llm(Settings.from_env({})), FakeReplayLLM)  # unset: not wrapped


def test_gemini_access_token_builds_a_vertex_client_with_those_credentials(monkeypatch):
    import sys
    import types

    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    class Credentials:
        def __init__(self, token):
            self.token = token

    google = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    genai.Client = Client
    google.genai = genai
    oauth2 = types.ModuleType("google.oauth2")
    credentials = types.ModuleType("google.oauth2.credentials")
    credentials.Credentials = Credentials
    for name, module in {"google": google, "google.genai": genai, "google.oauth2": oauth2, "google.oauth2.credentials": credentials}.items():
        monkeypatch.setitem(sys.modules, name, module)

    env = {"DL_LLM_BACKEND": "gemini", "DL_GEMINI_VERTEXAI": "1", "GOOGLE_CLOUD_PROJECT": "p", "GOOGLE_CLOUD_LOCATION": "global",
           "DL_GEMINI_ACCESS_TOKEN": "tok"}
    build_llm(Settings.from_env(env))
    assert seen["vertexai"] is True and seen["project"] == "p" and seen["location"] == "global"
    assert seen["credentials"].token == "tok"
    seen.clear()
    build_llm(Settings.from_env({k: v for k, v in env.items() if k != "DL_GEMINI_ACCESS_TOKEN"}))
    assert "credentials" not in seen  # unset: Application Default Credentials as before


# --- DL-032: Firestore ledger, sign-in settings and startup checks ---

def test_sign_in_and_store_settings_are_read_from_the_environment():
    defaults = Settings.from_env({})
    assert (defaults.store, defaults.auth_mode, defaults.daily_new_user_limit) == ("sqlite", "none", 100)
    s = Settings.from_env({"DL_STORE": "Firestore", "DL_AUTH_MODE": "firebase", "DL_FIREBASE_PROJECT_ID": "demo-proj",
                           "DL_FIREBASE_API_KEY": "web-key", "DL_DAILY_NEW_USER_LIMIT": "7"})
    assert (s.store, s.auth_mode, s.firebase_project_id, s.daily_new_user_limit) == ("firestore", "firebase", "demo-proj", 7)
    assert s.firestore is True and s.firebase_auth_domain == "demo-proj.firebaseapp.com"
    assert "web-key" not in repr(s)


@pytest.mark.parametrize("env", [
    {"DL_AUTH_MODE": "fake", "DL_PUBLIC_MODE": "1"},
    {"DL_AUTH_MODE": "firebase"},
    {"DL_AUTH_MODE": "magic"},
    {"DL_STORE": "postgres"},
])
def test_unsafe_or_incomplete_settings_refuse_to_serve(env):
    with pytest.raises(bootstrap.InvalidSettings):
        bootstrap.check_settings(Settings.from_env(env), env={})


@pytest.mark.parametrize("settings_env, process_env", [
    ({"DL_AUTH_MODE": "fake", "DL_STORE": "firestore"}, {}),
    ({"DL_AUTH_MODE": "fake"}, {"K_SERVICE": "decision-ledger"}),
])
def test_fake_sign_in_refuses_the_cloud_store_and_cloud_run(settings_env, process_env):
    with pytest.raises(bootstrap.InvalidSettings, match="local tests only"):
        bootstrap.check_settings(Settings.from_env(settings_env), env=process_env)


def test_fake_sign_in_runs_locally():
    bootstrap.check_settings(Settings.from_env({"DL_AUTH_MODE": "fake"}), env={})


def test_main_serve_exits_2_on_fake_sign_in_in_public_mode(monkeypatch, capsys):
    monkeypatch.setenv("DL_AUTH_MODE", "fake")
    monkeypatch.setenv("DL_PUBLIC_MODE", "1")
    monkeypatch.setattr(bootstrap, "uvicorn", SimpleNamespace(run=lambda *a, **k: pytest.fail("must not serve")))
    with pytest.raises(SystemExit) as exit_info:
        decision_ledger.main(["serve"])
    assert exit_info.value.code == 2 and "DL_AUTH_MODE" in capsys.readouterr().err


def test_identity_follows_the_auth_mode():
    from decision_ledger.adapters.firebase_auth import FakeIdentityVerifier, FirebaseIdentityVerifier

    assert bootstrap.build_identity(Settings.from_env({})) is None
    assert isinstance(bootstrap.build_identity(Settings.from_env({"DL_AUTH_MODE": "fake"})), FakeIdentityVerifier)
    firebase = bootstrap.build_identity(Settings.from_env({"DL_AUTH_MODE": "firebase", "DL_FIREBASE_PROJECT_ID": "demo"}))
    assert isinstance(firebase, FirebaseIdentityVerifier) and firebase.project_id == "demo"


def test_sign_in_mode_wires_the_provider_and_guards_the_showcase():
    provider, default = build_provider(Settings.from_env({"DL_DB_PATH": ":memory:", "DL_AUTH_MODE": "fake"}))
    assert provider.identity is not None and provider.projects.auth_required and provider.sandboxes.auth_required
    assert provider.projects.showcase_id == default.id


def test_local_mode_leaves_the_default_project_writable():
    provider, default = build_provider(Settings.from_env({"DL_DB_PATH": ":memory:"}))
    assert provider.identity is None and provider.projects.showcase_id is None
    assert provider.projects.authorize(default.id, None, write=True) == default


def test_runtime_reports_store_auth_mode_and_firebase_web_config():
    s = Settings.from_env({"DL_STORE": "firestore", "DL_AUTH_MODE": "firebase", "DL_FIREBASE_PROJECT_ID": "demo",
                           "DL_FIREBASE_API_KEY": "web-key"})
    runtime = bootstrap.build_runtime(s)
    assert runtime.store == "firestore" and runtime.auth_mode == "firebase"
    assert runtime.firebase == {"apiKey": "web-key", "authDomain": "demo.firebaseapp.com", "projectId": "demo"}
    assert bootstrap.build_runtime(Settings.from_env({})).firebase is None


def _fake_firestore_sdk(monkeypatch):
    """open_store builds FirestoreLedgerStore with the SDK helpers; the SDK is the optional cloud extra."""
    from decision_ledger.adapters import firestore_ledger
    from tests.fake_firestore import CONFLICT_ERRORS, FakeFieldFilter

    monkeypatch.setattr(firestore_ledger, "_sdk_field_filter", lambda: FakeFieldFilter)
    monkeypatch.setattr(firestore_ledger, "_sdk_conflict_errors", lambda: CONFLICT_ERRORS)


def test_firestore_store_is_opened_from_settings(monkeypatch):
    from decision_ledger.adapters.firestore_ledger import FirestoreLedgerStore
    from tests.fake_firestore import FakeFirestoreClient

    _fake_firestore_sdk(monkeypatch)
    monkeypatch.setattr(bootstrap, "_firestore_client", lambda settings: FakeFirestoreClient())
    store = bootstrap.open_store(Settings.from_env({"DL_STORE": "firestore"}))
    assert isinstance(store, FirestoreLedgerStore)


def test_showcase_on_firestore_is_rebuilt_once_and_kept_across_restarts(monkeypatch):
    from tests.fake_firestore import FakeFirestoreClient

    client = FakeFirestoreClient()
    _fake_firestore_sdk(monkeypatch)
    monkeypatch.setattr(bootstrap, "_firestore_client", lambda settings: client)
    from decision_ledger.adapters.memory import InMemorySandboxRegistry

    monkeypatch.setattr(bootstrap, "build_registry", lambda settings: InMemorySandboxRegistry())
    monkeypatch.setattr(bootstrap, "build_usage_counter", lambda settings, store: None)
    settings = Settings.from_env({"DL_STORE": "firestore", "DL_PUBLIC_MODE": "1"})
    provider, default = build_provider(settings)
    state = bootstrap.ShowcaseState()
    bootstrap.rebuild_showcase(provider, default.id, state)
    assert state.status == "ready"
    commits = client.commits
    again, default2 = build_provider(settings)  # a redeploy: same Firestore, new process
    state2 = bootstrap.ShowcaseState()
    bootstrap.rebuild_showcase(again, default2.id, state2)
    assert default2.id == default.id and state2.status == "ready" and client.commits == commits


def test_firestore_rules_deny_every_client_request():
    import re
    from pathlib import Path

    rules = (Path(__file__).resolve().parents[1] / "firestore.rules").read_text(encoding="utf-8")
    assert "match /{document=**}" in rules and re.search(r"allow\s+read,\s*write:\s*if\s+false;", rules)
    assert "if true" not in rules and "request.auth" not in rules
