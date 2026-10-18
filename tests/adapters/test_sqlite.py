import sqlite3

import pytest

from decision_ledger.adapters.storage.sqlite import SCHEMA_VERSION, SqliteLedgerStore
from decision_ledger.domain.errors import (
    DuplicatePrCheck,
    DuplicateSource,
    InvalidProjectOperation,
    NotFound,
    SchemaVersionMismatch,
)
from decision_ledger.domain.models import (
    AlertKind,
    AlertStatus,
    DecisionEvent,
    DecisionStatus,
    CheckableRule,
    PrCheck,
    Segment,
    Source,
    SourceKind,
)
from tests.factories import alert, decision, ev, topic


def firestore_store(client=None):
    from decision_ledger.adapters.firestore_ledger import FirestoreLedgerStore
    from tests.fake_firestore import CONFLICT_ERRORS, FakeFieldFilter, FakeFirestoreClient

    return FirestoreLedgerStore(client or FakeFirestoreClient(), field_filter=FakeFieldFilter, conflict_errors=CONFLICT_ERRORS)


# DL-032: the same storage contract runs against SQLite and the Firestore adapter (fake client).
@pytest.fixture(params=["sqlite", "firestore"])
def store(request, tmp_path):
    if request.param == "firestore":
        yield firestore_store()
        return
    s = SqliteLedgerStore(str(tmp_path / "ledger.db"))
    yield s
    s.close()


@pytest.fixture
def repo(store):
    return store.for_project(store.create_project("A", None).id)


def source(id="1", h="h1"):
    return Source(id=id, kind=SourceKind.TRANSCRIPT, title="週會", occurred_on="9/1", content_hash=h,
                  segments=[Segment(index=0, locator="00:01:00", speaker="Alice", text="那 v2 就定 10/15 上線")])


def test_schema_version_is_recorded_and_reopening_keeps_data(tmp_path):
    path = str(tmp_path / "v.db")
    first = SqliteLedgerStore(path)
    first.create_project("A", None)
    first.close()
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    again = SqliteLedgerStore(path)
    assert [p.name for p in again.list_projects()] == ["A"]
    again.close()


@pytest.mark.parametrize("version", [1, 2, 5])
def test_other_schema_version_is_refused_without_touching_the_file(tmp_path, version):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(f"CREATE TABLE sources (id TEXT PRIMARY KEY, content_hash TEXT UNIQUE, data TEXT); PRAGMA user_version = {version};")
    conn.close()

    def state():
        c = sqlite3.connect(path)
        try:
            return c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(), c.execute("PRAGMA user_version").fetchone()[0]
        finally:
            c.close()

    before = state()
    with pytest.raises(SchemaVersionMismatch) as err:
        SqliteLedgerStore(path)
    assert (err.value.path, err.value.found, err.value.expected) == (path, version, SCHEMA_VERSION)
    assert state() == before


def test_source_round_trip_with_segments(repo):
    with repo.unit_of_work() as w:
        w.save_source(source())
    assert repo.get_source("1").segments[0].locator == "00:01:00"
    assert repo.find_source_by_hash("h1").id == "1"
    assert repo.get_segments("1")[0].text.startswith("那 v2")
    assert [s.id for s in repo.list_sources()] == ["1"]
    assert repo.find_source_by_hash("nope") is None


def test_duplicate_content_hash_raises_domain_error_and_rolls_back(repo):
    with repo.unit_of_work() as w:
        w.save_source(source())
    with pytest.raises(DuplicateSource):
        with repo.unit_of_work() as w:
            w.save_decision(decision("9"))
            w.save_source(source(id="2"))
    assert repo.get_decision("9") is None
    assert len(repo.list_sources()) == 1


def test_exception_rolls_back_all_writes(repo):
    with pytest.raises(RuntimeError):
        with repo.unit_of_work() as w:
            w.save_source(source())
            w.save_decision(decision("1"))
            raise RuntimeError("boom")
    assert repo.list_sources() == [] and repo.list_decisions() == []


def test_allocate_id_is_sequential_per_namespace_and_rolls_back(repo):
    with repo.unit_of_work() as w:
        assert [w.allocate_id("decision"), w.allocate_id("decision"), w.allocate_id("alert")] == ["1", "2", "1"]
    with pytest.raises(RuntimeError):
        with repo.unit_of_work() as w:
            w.allocate_id("decision")
            raise RuntimeError
    with repo.unit_of_work() as w:
        assert w.allocate_id("decision") == "3"


def test_decisions_evidence_and_status_filters(repo):
    rule = CheckableRule(description="r", path_patterns=["src/*"])
    with repo.unit_of_work() as w:
        w.save_decision(decision("1"))
        w.save_decision(decision("2", status=DecisionStatus.SUPERSEDED, rule=rule))
        w.save_decision(decision("3", rule=rule))
        w.add_evidence("1", [ev("第二個出處", locator="9/16")])
        assert w.get_decision("1").evidence[-1].quote == "第二個出處"
    assert len(repo.get_decision("1").evidence) == 2
    assert [d.id for d in repo.list_decisions([DecisionStatus.ACTIVE])] == ["1", "3"]
    assert [d.id for d in repo.list_checkable_decisions()] == ["3"]


def test_search_ranks_by_term_hits_and_supports_cjk_bigrams(repo):
    with repo.unit_of_work() as w:
        w.save_decision(decision("1", statement="v2 上線延到 11/1", topic="v2 上線時程"))
        w.save_decision(decision("2", statement="前端改用 Next.js", topic="前端技術選型"))
        w.save_decision(decision("3", statement="100%_literal", topic="x"))
    assert [d.id for d in repo.search_decisions(["v2", "延到"], 5)] == ["1"]
    assert [d.id for d in repo.search_decisions(["next.js"], 5)] == ["2"]
    assert [d.id for d in repo.search_decisions(["%_"], 5)] == ["3"]  # LIKE wildcards are matched literally
    assert repo.search_decisions([], 5) == []
    assert repo.search_decisions(["不存在"], 5) == []


def test_alerts_update_lock_and_obsolete(repo):
    with repo.unit_of_work() as w:
        w.save_alert(alert("1", AlertKind.CONFLICT, ["1", "2"]))
        w.save_alert(alert("2", AlertKind.ASSUMPTION, ["2"]))
        w.save_alert(alert("3", AlertKind.ASSUMPTION, ["3"]))
    with repo.unit_of_work() as w:
        assert w.get_alert_for_update("1").kind is AlertKind.CONFLICT
        assert w.obsolete_alerts_of(["2"], except_alert_id="1") == ["2"]
    assert repo.get_alert("2").status is AlertStatus.OBSOLETE
    assert [a.id for a in repo.list_alerts(AlertStatus.OPEN)] == ["1", "3"]
    assert len(repo.list_alerts()) == 3


def test_events_are_appended_in_order(repo):
    with repo.unit_of_work() as w:
        w.save_decision(decision("1"))  # events are scoped through the project of their decision
        w.append_event(DecisionEvent(decision_id="1", kind="created"))
        w.append_event(DecisionEvent(decision_id="1", kind="reconsider", note="n"))
        w.append_event(DecisionEvent(decision_id="2", kind="created"))
    events = repo.list_events("1")
    assert [e.kind for e in events] == ["created", "reconsider"] and events[0].id is not None


def test_pr_check_upsert_same_check_but_reject_duplicate_key(repo):
    check = PrCheck(key="k", diff="d", comment="c", violated=True, alert_ids=["1"])
    with repo.unit_of_work() as w:
        w.save_pr_check(check)
    with repo.unit_of_work() as w:
        w.save_pr_check(check.model_copy(update={"comment_published": True}))
    assert repo.get_pr_check("k").comment_published is True
    with pytest.raises(DuplicatePrCheck):
        with repo.unit_of_work() as w:
            w.save_pr_check(PrCheck(key="k", diff="other", comment="c2", violated=False))
    assert repo.get_pr_check("k").diff == "d"
    assert [c.key for c in repo.list_pr_checks()] == ["k"]
    assert repo.get_pr_check("missing") is None


def test_in_memory_database_works():
    s = SqliteLedgerStore(":memory:")
    r = s.for_project(s.create_project("A", None).id)
    with r.unit_of_work() as w:
        w.save_decision(decision("1"))
    assert r.get_decision("1") is not None
    s.close()


def test_search_with_very_many_terms_does_not_crash(repo):
    """Verify 3/5 (R2-3): a long question must not exceed SQLite's expression depth."""
    with repo.unit_of_work() as w:
        w.save_decision(decision("1", statement="v2 上線延到 11/1"))
    terms = [f"w{i}" for i in range(1100)] + ["v2"]
    assert [d.id for d in repo.search_decisions(terms, 5)] in ([], ["1"])
    assert [d.id for d in repo.search_decisions(["v2", *terms], 5)] == ["1"]


def test_search_keeps_terms_beyond_the_first_forty(repo):
    """Review R4-1: a long question's key term late in the list must still match."""
    with repo.unit_of_work() as w:
        w.save_decision(decision("1", statement="report-service 走 internal API", topic="服務架構"))
    noise = [f"雜{i}" for i in range(60)]
    assert [d.id for d in repo.search_decisions([*noise, "internal"], 5)] == ["1"]


# --- projects -------------------------------------------------------------------


def test_projects_create_lookup_and_uniqueness(store):
    a = store.create_project("A", "northwind/v2")
    b = store.create_project("B", None)
    store.create_project("C", None)  # several projects without a repo are fine
    assert (a.id, b.id) == ("P1", "P2")
    assert store.get_project("P2") == b and store.get_project("P9") is None
    assert store.project_by_repo("northwind/v2") == a and store.project_by_repo("x/y") is None
    with pytest.raises(InvalidProjectOperation):
        store.create_project("A", None)  # names stay unique
    d = store.create_project("D", "northwind/v2")  # DL-032: a repo may be registered by several projects
    assert store.projects_by_repo("northwind/v2") == [a, d] and store.projects_by_repo("x/y") == []
    assert [p.name for p in store.list_projects()] == ["A", "B", "C", "D"]


def test_owner_lookup_and_project_update(store):
    """DL-032: a signed-in user's workspace is found by owner; its repo can be changed."""
    w = store.create_project("Workspace w1", None, project_id="w1aaaaaaaaaaaaaa", owner_uid="uid-1")
    store.create_project("Other", None)
    assert store.project_by_owner("uid-1") == w and store.project_by_owner("uid-2") is None
    store.update_project(w.model_copy(update={"repo": "acme/demo"}))
    assert store.get_project(w.id).repo == "acme/demo" and store.projects_by_repo("acme/demo") == [store.get_project(w.id)]
    assert store.get_project(w.id).owner_uid == "uid-1"


def test_list_projects_counts_each_projects_open_alerts(store):
    a, b = (store.for_project(store.create_project(n, None).id) for n in "AB")
    with a.unit_of_work() as w:
        w.save_alert(alert("1", project_id=a.project_id))
        w.save_alert(alert("2", status=AlertStatus.RESOLVED, project_id=a.project_id))
    with b.unit_of_work() as w:
        w.save_alert(alert("3", project_id=b.project_id))
        w.save_alert(alert("4", project_id=b.project_id))
    assert {p.name: p.pending for p in store.list_projects()} == {"A": 1, "B": 2}


def test_list_projects_can_be_limited_to_named_projects(store):
    """DL-032 review R1-03: a signed-in page lists only the showcase and its own workspace."""
    a, b, c = (store.create_project(n, None) for n in "ABC")
    with store.for_project(c.id).unit_of_work() as w:
        w.save_alert(alert("1", project_id=c.id))
    assert [(p.name, p.pending) for p in store.list_projects([c.id, a.id, "nope", a.id])] == [("A", 0), ("C", 1)]
    assert store.list_projects([]) == [] and len(store.list_projects()) == 3


def test_updating_an_unknown_project_is_not_found(store):
    """DL-032 review R1-09: every store refuses to update (or silently create) a project that does not exist."""
    ghost = store.create_project("A", None).model_copy(update={"id": "P99", "repo": "acme/x"})
    with pytest.raises(NotFound):
        store.update_project(ghost)
    assert store.get_project("P99") is None and store.projects_by_repo("acme/x") == []


# --- isolation between projects -------------------------------------------------


@pytest.fixture
def two(store):
    """Project A holds one of everything; project B is empty."""
    a = store.for_project(store.create_project("A", None).id)
    b = store.for_project(store.create_project("B", None).id)
    with a.unit_of_work() as w:
        w.save_topic(topic("T1", "v2 上線時程"))
        w.save_source(source())
        w.save_decision(decision("1", rule=CheckableRule(description="r", path_patterns=["src/*"])))
        w.save_alert(alert("1"))
        w.append_event(DecisionEvent(decision_id="1", kind="created"))
        w.save_pr_check(PrCheck(key="k", diff="d", comment="c", violated=False))
    return a, b


READS = {
    "find_source_by_hash": lambda r: r.find_source_by_hash("h1"),
    "get_source": lambda r: r.get_source("1"),
    "list_sources": lambda r: r.list_sources(),
    "get_segments": lambda r: r.get_segments("1"),
    "get_decision": lambda r: r.get_decision("1"),
    "list_decisions": lambda r: r.list_decisions(),
    "list_decisions_by_status": lambda r: r.list_decisions([DecisionStatus.ACTIVE]),
    "list_checkable_decisions": lambda r: r.list_checkable_decisions(),
    "search_decisions": lambda r: r.search_decisions(["v2"], 5),
    "get_alert": lambda r: r.get_alert("1"),
    "list_alerts": lambda r: r.list_alerts(),
    "list_open_alerts": lambda r: r.list_alerts(AlertStatus.OPEN),
    "list_events": lambda r: r.list_events("1"),
    "get_pr_check": lambda r: r.get_pr_check("k"),
    "list_pr_checks": lambda r: r.list_pr_checks(),
    "list_topics": lambda r: r.list_topics(include_merged=True),
    "get_topic": lambda r: r.get_topic("T1"),
    "resolve_topic": lambda r: r.resolve_topic("T1"),
    "find_topic_by_name": lambda r: r.find_topic_by_name("v2 上線時程"),
}

WRITER_READS = {
    "get_decision": lambda w: w.get_decision("1"),
    "get_alert_for_update": lambda w: w.get_alert_for_update("1"),
    "obsolete_alerts_of": lambda w: w.obsolete_alerts_of(["1"], except_alert_id=None),
    "get_topic": lambda w: w.get_topic("T1"),
    "resolve_topic": lambda w: w.resolve_topic("T1"),
    "find_topic_by_name": lambda w: w.find_topic_by_name("v2 上線時程"),
}


@pytest.mark.parametrize("read", READS.values(), ids=READS.keys())
def test_every_repository_read_stays_inside_its_project(two, read):
    a, b = two
    assert read(a)  # the data exists...
    assert not read(b)  # ...but project B cannot see it


@pytest.mark.parametrize("read", WRITER_READS.values(), ids=WRITER_READS.keys())
def test_every_writer_read_stays_inside_its_project(two, read):
    a, b = two
    with b.unit_of_work() as w:
        assert not read(w)
    assert a.get_alert("1").status is AlertStatus.OPEN  # B's obsolete_alerts_of did not reach A


def test_same_ids_in_two_projects_are_separate_rows(two):
    """Ids are numbered per project: project B's #1 never touches project A's #1."""
    a, b = two
    with b.unit_of_work() as w:
        w.save_topic(topic("T1", "B 的主題"))
        w.save_decision(decision("1", statement="B 的決策"))
        w.save_alert(alert("1"))
    with b.unit_of_work() as w:
        w.save_decision(w.get_decision("1").model_copy(update={"status": DecisionStatus.SUPERSEDED}))
    assert a.get_decision("1").statement == "v2 在 10/15 上線" and a.get_decision("1").status is DecisionStatus.ACTIVE
    assert b.get_decision("1").statement == "B 的決策"
    assert a.get_topic("T1").name == "v2 上線時程" and b.get_topic("T1").name == "B 的主題"
    assert a.list_events("1") and b.list_events("1") == []


def test_ids_are_numbered_per_project(store):
    a = store.for_project(store.create_project("A", None).id)
    b = store.for_project(store.create_project("B", None).id)
    with a.unit_of_work() as w:
        assert [w.allocate_id("decision"), w.allocate_id("decision"), w.allocate_id("alert")] == ["1", "2", "1"]
    with b.unit_of_work() as w:
        assert [w.allocate_id("decision"), w.allocate_id("topic"), w.allocate_id("source")] == ["1", "1", "1"]
    assert store.create_project("C", None).id == "P3"  # project ids stay database-wide


def test_same_ids_never_leak_between_projects(store):
    """Both projects hold #1 of everything; every read returns only the caller's own rows."""
    repos = [store.for_project(store.create_project(n, None).id) for n in "AB"]
    for repo in repos:
        tag = repo.project_id
        with repo.unit_of_work() as w:
            w.save_topic(topic(w.allocate_id("topic"), f"{tag} 主題"))
            sid = w.allocate_id("source")
            w.save_source(source(id=sid).model_copy(update={"title": f"{tag} 週會"}))
            did = w.allocate_id("decision")
            w.save_decision(decision(did, topic_id="1", statement=f"{tag} 決策", rule=CheckableRule(description="r", path_patterns=["src/*"])))
            w.save_alert(alert(w.allocate_id("alert"), decision_ids=[did]))
            w.append_event(DecisionEvent(decision_id=did, kind="created", note=tag))
            w.save_pr_check(PrCheck(key="k", diff=tag, comment="c", violated=False))
    for repo in repos:
        tag = repo.project_id
        assert repo.get_decision("1").statement == f"{tag} 決策"
        assert [d.statement for d in repo.list_decisions()] == [f"{tag} 決策"]
        assert repo.get_source("1").title == f"{tag} 週會" and len(repo.get_segments("1")) == 1
        assert [e.note for e in repo.list_events("1")] == [tag]
        assert repo.get_topic("1").name == f"{tag} 主題" and repo.get_pr_check("k").diff == tag
        assert [a.project_id for a in repo.list_alerts()] == [tag]
        with repo.unit_of_work() as w:
            assert [d.statement for d in w.list_topic_decisions("1")] == [f"{tag} 決策"]
            assert w.get_decision("1").project_id == tag


def test_same_source_and_same_pr_succeed_in_each_project(two):
    a, b = two
    with b.unit_of_work() as w:
        w.save_source(source(id="2"))  # same content hash as A's source 1
        w.save_pr_check(PrCheck(key="k", diff="d2", comment="c", violated=True))
    assert b.find_source_by_hash("h1").id == "2" and a.find_source_by_hash("h1").id == "1"
    assert (a.get_pr_check("k").diff, b.get_pr_check("k").diff) == ("d", "d2")
    assert a.list_sources()[0].project_id == a.project_id and b.list_sources()[0].project_id == b.project_id


# --- topics -------------------------------------------------------------------


def test_topic_lookup_by_normalized_name_ignores_merged_topics(repo):
    with repo.unit_of_work() as w:
        w.save_topic(topic("T1", "API 規範"))
        w.save_topic(topic("T2", "舊名", merged_into="T1"))
    assert repo.find_topic_by_name("ａｐｉ規範").id == "T1"
    assert repo.find_topic_by_name("舊名") is None
    assert [t.id for t in repo.list_topics()] == ["T1"]
    assert repo.resolve_topic("T2").id == "T1"


def test_merge_moves_decisions_repoints_chains_and_refreshes_search_text(repo):
    with repo.unit_of_work() as w:
        for tid, name in [("T1", "DB 選擇"), ("T2", "資料庫選型"), ("T3", "基礎設施")]:
            w.save_topic(topic(tid, name))
        w.save_decision(decision("1", topic_id="T1", statement="用 PostgreSQL"))
        w.save_decision(decision("2", topic_id="T2", statement="改用 MySQL"))
    with repo.unit_of_work() as w:
        w.merge_topic("T1", "T2")
    with repo.unit_of_work() as w:
        w.merge_topic("T2", "T3")
    assert {d.id: d.topic_id for d in repo.list_decisions()} == {"1": "T3", "2": "T3"}
    assert {t.id: t.merged_into for t in repo.list_topics(include_merged=True)} == {"T1": "T3", "T2": "T3", "T3": None}
    assert [d.id for d in repo.search_decisions(["基礎"], 5)] == ["1", "2"]
    assert repo.search_decisions(["選擇"], 5) == []


def test_rename_refreshes_search_text_in_the_same_transaction(repo):
    with repo.unit_of_work() as w:
        w.save_topic(topic("T1", "DB 選擇"))
        w.save_decision(decision("1", topic_id="T1", statement="用 PostgreSQL"))
    with repo.unit_of_work() as w:
        w.rename_topic("T1", "資料庫選型")
    assert repo.get_topic("T1").name == "資料庫選型"
    assert [d.id for d in repo.search_decisions(["選型"], 5)] == ["1"]
    assert repo.find_topic_by_name("DB 選擇") is None


def test_reset_project_clears_only_that_project(store):
    from decision_ledger.bootstrap import ContainerFactory, run_demo, DEMO_DIR
    from decision_ledger.adapters.llm.fake import FakeReplayLLM
    from tests.fakes import FakePullRequestSource, RecordingPublisher

    a, b = store.create_project("A", None), store.create_project("B", None)
    factory = ContainerFactory(store, FakeReplayLLM(str(DEMO_DIR / "llm")), RecordingPublisher(), FakePullRequestSource())
    run_demo(factory.for_project(a.id))
    run_demo(factory.for_project(b.id))
    keep = len(store.for_project(b.id).list_decisions())
    store.reset_project(a.id)
    ra = store.for_project(a.id)
    assert ra.list_sources() == [] and ra.list_decisions() == [] and ra.list_alerts() == [] and ra.list_pr_checks() == []
    assert ra.list_topics(include_merged=True) == []
    assert store.get_project(a.id) is not None
    assert len(store.for_project(b.id).list_decisions()) == keep
    # numbering restarts, so the recorded demo fits the reset project again
    run_demo(factory.for_project(a.id))
    assert len(ra.list_decisions()) == keep


def test_create_project_with_given_id(store):
    p = store.create_project("Sandbox abc", None, project_id="abcdefghijklmnop")
    assert p.id == "abcdefghijklmnop" and store.get_project(p.id).name == "Sandbox abc"
    with pytest.raises(InvalidProjectOperation):
        store.create_project("Other", None, project_id="abcdefghijklmnop")
    assert store.create_project("Next", None).id.startswith("P")



def test_merge_then_rename_in_one_transaction_sees_its_own_writes(repo):
    """DL-032: queries inside a unit of work see the writes made earlier in it (Firestore buffers them)."""
    with repo.unit_of_work() as w:
        w.save_topic(topic("T1", "資料庫選型"))
        w.save_topic(topic("T2", "DB 選擇"))
        w.save_decision(decision("1", topic_id="T1", statement="用 PostgreSQL"))
        w.save_decision(decision("2", topic_id="T2", statement="不用 MongoDB"))
        w.merge_topic("T2", "T1")
        assert [d.id for d in w.list_topic_decisions("T1")] == ["1", "2"]
        w.rename_topic("T1", "儲存方案")
        assert w.find_topic_by_name("儲存方案").id == "T1" and w.find_topic_by_name("DB 選擇") is None
    assert [d.id for d in repo.search_decisions(["儲存方案"], 5)] == ["1", "2"]


def test_version_3_database_migrates_to_4_and_allows_shared_repos(tmp_path):
    """DL-032: the previous schema (repo UNIQUE) is migrated in place; data and foreign keys survive."""
    from decision_ledger.adapters.storage import sqlite as mod

    path = str(tmp_path / "v3.db")
    v3 = mod.SCHEMA.replace("repo TEXT,", "repo TEXT UNIQUE,", 1)
    assert v3 != mod.SCHEMA
    conn = sqlite3.connect(path)
    conn.executescript(f"BEGIN; {v3} PRAGMA user_version = 3; COMMIT;")
    project = mod.Project(id="P1", name="A", repo="northwind/v2")
    conn.execute("INSERT INTO projects (id, seq, name, repo, data) VALUES ('P1', 1, 'A', 'northwind/v2', ?)", (project.model_dump_json(),))
    conn.execute("INSERT INTO counters (project_id, namespace, value) VALUES ('', 'project', 1)")
    conn.execute("INSERT INTO topics (project_id, id, seq, norm_name, merged_into, data) VALUES ('P1', 'T1', 1, 'x', NULL, '{}')")
    conn.commit()
    conn.close()
    store = SqliteLedgerStore(path)
    assert store.get_project("P1").repo == "northwind/v2"
    store.create_project("B", "northwind/v2")
    assert [p.name for p in store.projects_by_repo("northwind/v2")] == ["A", "B"]
    store.close()
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 4
    assert check.execute("PRAGMA foreign_key_check").fetchall() == []
    assert check.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 1
    check.close()
