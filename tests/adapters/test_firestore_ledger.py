"""DL-032: Firestore ledger behaviour beyond the shared storage contract (see test_sqlite.py)."""

import pytest

from decision_ledger.adapters.firestore_ledger import DIFF_TRUNCATED, MAX_SOURCE_BYTES, MAX_WRITES, FirestoreLedgerStore
from decision_ledger.domain.errors import ConcurrentModification, InvalidProjectOperation, ParseError
from decision_ledger.domain.models import PrCheck, Segment, Source, SourceKind
from tests.factories import decision
from tests.fake_firestore import CONFLICT_ERRORS, FakeFieldFilter, FakeFirestoreClient


def make(client):
    return FirestoreLedgerStore(client, field_filter=FakeFieldFilter, conflict_errors=CONFLICT_ERRORS)


def test_data_and_numbering_survive_a_redeploy():
    """A new store on the same Firestore (a new Cloud Run instance) reads everything back and keeps numbering."""
    client = FakeFirestoreClient()
    first = make(client)
    project = first.create_project("A", "acme/app", owner_uid="uid-1")
    repo = first.for_project(project.id)
    with repo.unit_of_work() as w:
        w.save_decision(decision(w.allocate_id("decision"), statement="report-service goes through the internal API"))
    again = make(client)
    assert again.get_project(project.id) == project and again.project_by_owner("uid-1") == project
    repo2 = again.for_project(project.id)
    assert [d.statement for d in repo2.list_decisions()] == ["report-service goes through the internal API"]
    with repo2.unit_of_work() as w:
        assert w.allocate_id("decision") == "2"
    assert again.create_project("B", None).id == "P2"


def test_a_concurrent_commit_from_another_instance_is_rejected_whole():
    client = FakeFirestoreClient()
    a, b = make(client), make(client)
    pid = a.create_project("A", None).id
    with b.for_project(pid).unit_of_work() as w:  # creates the version document
        w.save_decision(decision("1"))
    ra, rb = a.for_project(pid), b.for_project(pid)
    with pytest.raises(ConcurrentModification):
        with ra.unit_of_work() as wa:
            wa.save_decision(decision("2"))
            with rb.unit_of_work() as wb:  # another instance commits first
                wb.save_decision(decision("3"))
    assert sorted(d.id for d in ra.list_decisions()) == ["1", "3"]


def test_first_commits_race_on_the_version_document():
    client = FakeFirestoreClient()
    a, b = make(client), make(client)
    pid = a.create_project("A", None).id
    with pytest.raises(ConcurrentModification):
        with a.for_project(pid).unit_of_work() as wa:
            wa.save_decision(decision("1"))
            with b.for_project(pid).unit_of_work() as wb:
                wb.save_decision(decision("2"))
    assert [d.id for d in a.for_project(pid).list_decisions()] == ["2"]


def test_failed_commit_writes_nothing_and_ids_roll_back():
    client = FakeFirestoreClient()
    store = make(client)
    repo = store.for_project(store.create_project("A", None).id)
    client.fail_next_commit = CONFLICT_ERRORS[1]("boom")
    with pytest.raises(ConcurrentModification):
        with repo.unit_of_work() as w:
            w.save_decision(decision(w.allocate_id("decision")))
    assert repo.list_decisions() == []
    with repo.unit_of_work() as w:
        assert w.allocate_id("decision") == "1"


def test_read_only_unit_of_work_commits_nothing():
    client = FakeFirestoreClient()
    store = make(client)
    repo = store.for_project(store.create_project("A", None).id)
    before = client.commits
    with repo.unit_of_work() as w:
        w.get_decision("1")
    assert client.commits == before


def test_too_many_writes_in_one_unit_are_refused_and_nothing_is_written():
    store = make(FakeFirestoreClient())
    repo = store.for_project(store.create_project("A", None).id)
    with pytest.raises(ParseError):
        with repo.unit_of_work() as w:
            for i in range(MAX_WRITES + 1):
                w.save_decision(decision(str(i + 1)))
    assert repo.list_decisions() == []


def test_a_source_too_large_for_one_document_is_refused():
    store = make(FakeFirestoreClient())
    repo = store.for_project(store.create_project("A", None).id)
    huge = Source(id="1", kind=SourceKind.TRANSCRIPT, title="t", content_hash="h",
                  segments=[Segment(index=0, locator="0", speaker="A", text="x" * 950_000)])
    with pytest.raises(ParseError):
        with repo.unit_of_work() as w:
            w.save_source(huge)
    assert repo.list_sources() == []


def test_project_names_stay_unique_across_instances():
    client = FakeFirestoreClient()
    make(client).create_project("Workspace", None, project_id="w1")
    with pytest.raises(InvalidProjectOperation):
        make(client).create_project("Workspace", None, project_id="w2")
    with pytest.raises(InvalidProjectOperation):
        make(client).create_project("Other", None, project_id="w1")


def test_layout_is_rooted_at_projects():
    client = FakeFirestoreClient()
    store = make(client)
    pid = store.create_project("A", None).id
    with store.for_project(pid).unit_of_work() as w:
        w.save_decision(decision("1"))
    paths = client.paths()
    assert f"projects/{pid}" in paths and f"projects/{pid}/decisions/1" in paths and f"projects/{pid}/meta/version" in paths
    assert all(p.startswith(("projects/", "ledger_meta/")) for p in paths)


def test_listing_named_projects_reads_only_those():
    """DL-032 review R1-03: the signed-in project list costs the same with 3 projects or 200."""
    client = FakeFirestoreClient()
    store = make(client)
    ids = [store.create_project(f"W{i}", None).id for i in range(200)]
    client.reads = 0
    assert [p.id for p in store.list_projects([ids[0], ids[150]])] == [ids[0], ids[150]]
    assert client.reads <= 4


def test_an_oversized_pr_diff_is_stored_cut_not_lost():
    """DL-032 review R1-02: a PR larger than a Firestore document keeps its head (all the judge reads) and a marker."""
    client = FakeFirestoreClient()
    store = make(client)
    repo = store.for_project(store.create_project("A", None).id)
    head = "diff --git a/x.py b/x.py\n+import db_client\n"
    big = head + "".join(f'+line {i} "quoted" text\n' for i in range(60_000))
    with repo.unit_of_work() as w:
        w.save_pr_check(PrCheck(key="k", diff=big, comment="c", violated=True))
        w.save_pr_check(PrCheck(key="small", diff=head, comment="c", violated=False))
    stored = repo.get_pr_check("k")
    assert len(big.encode()) > MAX_SOURCE_BYTES and stored.diff.startswith(head) and stored.diff.endswith(DIFF_TRUNCATED)
    assert len(stored.model_dump_json().encode()) <= MAX_SOURCE_BYTES and stored.comment == "c" and stored.violated
    assert stored.diff.removesuffix(DIFF_TRUNCATED).endswith("\n")  # cut at a line boundary
    assert repo.get_pr_check("small").diff == head


def test_any_oversized_record_refuses_the_unit_of_work_with_a_parse_error():
    """Review R2-01: a topic name, note or evidence too large for one document is a 422, and nothing is written."""
    store = make(FakeFirestoreClient())
    repo = store.for_project(store.create_project("A", None).id)
    with repo.unit_of_work() as w:
        w.save_decision(decision("1"))
    with pytest.raises(ParseError, match="too large"):
        with repo.unit_of_work() as w:
            w.save_decision(decision("2"))
            w.save_decision(decision("1", statement="x" * (MAX_SOURCE_BYTES + 1)))
    assert [d.id for d in repo.list_decisions()] == ["1"] and repo.get_decision("1").statement == decision("1").statement


@pytest.mark.parametrize("slack", [-120, -60, -1, 0, 1, 60])
def test_a_pr_check_just_under_or_over_the_budget_is_kept(slack):
    """Review R3-02: the cut uses the writer's own measure, so no check near the limit is refused by the writer."""
    from decision_ledger.adapters.firestore_ledger import _fields_bytes, _pr_fields

    store = make(FakeFirestoreClient())
    repo = store.for_project(store.create_project("A", None).id)
    base = PrCheck(key="k", diff="", comment="c", violated=False)
    pad = MAX_SOURCE_BYTES - _fields_bytes(_pr_fields(base)) + slack
    check = base.model_copy(update={"diff": "+" * pad})  # one byte per character in JSON, so slack lands exactly
    with repo.unit_of_work() as w:
        w.save_pr_check(check)
    stored = repo.get_pr_check("k")
    assert _fields_bytes(_pr_fields(stored)) <= MAX_SOURCE_BYTES
    assert stored.diff == check.diff or stored.diff.endswith(DIFF_TRUNCATED)
