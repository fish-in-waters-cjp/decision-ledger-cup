"""DL-001: rebuild the showcase project from the recorded scenario on cold start."""

import threading

import pytest

from decision_ledger import bootstrap
from decision_ledger.bootstrap import Settings, build_provider, rebuild_showcase, showcase_complete
from decision_ledger.domain.models import AlertKind, AlertStatus, SourceKind
from decision_ledger.services.container import ShowcaseState


def provider(tmp_path, **env):
    return build_provider(Settings.from_env({"DL_DB_PATH": str(tmp_path / "s.db"), **env}))


class ExplodingLLM:
    name = "gemini"

    def __init__(self):
        self.calls = 0

    def generate_json(self, *a, **k):
        self.calls += 1
        raise AssertionError("rebuild must not call the configured LLM")


def open_kinds(factory, pid):
    return {a.kind for a in factory.store.for_project(pid).list_alerts(AlertStatus.OPEN)}


def test_empty_showcase_is_rebuilt_with_all_alert_kinds_and_no_live_llm(tmp_path):
    factory, project = provider(tmp_path)
    live = factory.llm = ExplodingLLM()
    state = ShowcaseState()
    rebuild_showcase(factory, project.id, state)
    assert state.status == "ready" and live.calls == 0
    assert AlertKind.ASSUMPTION in open_kinds(factory, project.id)
    assert showcase_complete(factory.store, project.id)


def test_rebuilt_showcase_shows_conflict_and_deviation(tmp_path):
    factory, project = provider(tmp_path)
    rebuild_showcase(factory, project.id, ShowcaseState())
    assert {AlertKind.CONFLICT, AlertKind.DEVIATION} <= open_kinds(factory, project.id)


def test_complete_showcase_is_left_alone(tmp_path):
    factory, project = provider(tmp_path)
    rebuild_showcase(factory, project.id, ShowcaseState())
    repo = factory.store.for_project(project.id)
    before = (len(repo.list_sources()), len(repo.list_decisions()), len(repo.list_alerts()))
    state = ShowcaseState()
    rebuild_showcase(factory, project.id, state)
    assert state.status == "ready"
    assert (len(repo.list_sources()), len(repo.list_decisions()), len(repo.list_alerts())) == before


def test_half_built_showcase_is_cleared_then_rebuilt(tmp_path):
    factory, project = provider(tmp_path)
    container = factory.for_project(project.id)
    manifest_step = (bootstrap.DEMO_DIR / "01-kickoff-0908.txt").read_text(encoding="utf-8")
    container.ingestion.ingest(SourceKind.TRANSCRIPT, "partial", manifest_step, None)
    assert not showcase_complete(factory.store, project.id)
    state = ShowcaseState()
    rebuild_showcase(factory, project.id, state)
    assert state.status == "ready" and showcase_complete(factory.store, project.id)
    titles = [s.title for s in factory.store.for_project(project.id).list_sources()]
    assert "partial" not in titles


def test_rebuild_failure_is_reported_not_raised(tmp_path, monkeypatch):
    factory, project = provider(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("recorded responses do not match")

    monkeypatch.setattr(bootstrap, "run_demo", boom)
    state = ShowcaseState()
    rebuild_showcase(factory, project.id, state)
    assert state.status == "failed"


def test_rebuild_failing_midway_leaves_no_half_built_showcase(tmp_path, monkeypatch):
    factory, project = provider(tmp_path)
    real = bootstrap.run_demo

    def half(container, demo_dir=bootstrap.DEMO_DIR):
        raw = (demo_dir / "01-weekly-0901.txt").read_text(encoding="utf-8")
        container.ingestion.ingest(SourceKind.TRANSCRIPT, "v2 產品週會", raw, "9/1")
        raise RuntimeError("second step failed")

    monkeypatch.setattr(bootstrap, "run_demo", half)
    state = ShowcaseState()
    rebuild_showcase(factory, project.id, state)
    repo = factory.store.for_project(project.id)
    assert state.status == "failed" and repo.list_sources() == [] and repo.list_decisions() == []
    monkeypatch.setattr(bootstrap, "run_demo", real)


def test_concurrent_rebuilds_run_once(tmp_path, monkeypatch):
    factory, project = provider(tmp_path)
    real, runs = bootstrap.run_demo, []

    def counted(*a, **k):
        runs.append(1)
        return real(*a, **k)

    monkeypatch.setattr(bootstrap, "run_demo", counted)
    threads = [threading.Thread(target=rebuild_showcase, args=(factory, project.id, ShowcaseState())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(runs) == 1


def test_settings_for_firestore_and_rebuild():
    assert Settings.from_env({}).firestore is False and Settings.from_env({}).showcase_rebuild is False
    s = Settings.from_env({"DL_FIRESTORE": "1", "DL_SHOWCASE_REBUILD": "1"})
    assert s.firestore is True and s.showcase_rebuild is True
    assert Settings.from_env({"DL_PUBLIC_MODE": "1"}).showcase_rebuild is True


def test_registry_defaults_to_memory_and_counter_to_the_sqlite_store(tmp_path):
    from decision_ledger.adapters.memory import InMemorySandboxRegistry
    from decision_ledger.adapters.storage.sqlite import SqliteLedgerStore

    s = Settings.from_env({})
    store = SqliteLedgerStore(str(tmp_path / "ledger.db"))
    assert isinstance(bootstrap.build_registry(s), InMemorySandboxRegistry)
    assert bootstrap.build_usage_counter(s, store) is store


def test_counter_uses_firestore_when_enabled(monkeypatch):
    from decision_ledger.adapters.firestore_usage import FirestoreUsageCounter

    from decision_ledger.adapters import firestore_usage

    monkeypatch.setattr(bootstrap, "_firestore_client", lambda settings: object())
    monkeypatch.setattr(firestore_usage, "firestore_increment", lambda: (lambda n: n))  # the SDK is an optional extra
    s = Settings.from_env({"DL_FIRESTORE": "1"})
    counter = bootstrap.build_usage_counter(s, store=None)  # type: ignore[arg-type]
    assert isinstance(counter, FirestoreUsageCounter)
