import pytest

from decision_ledger.domain.errors import InvalidProjectOperation, NotFound
from decision_ledger.services.projects import ProjectService
from tests.factories import alert
from tests.fakes import FakeLedgerStore


def service():
    store = FakeLedgerStore()
    return ProjectService(store), store


def test_create_trims_and_returns_the_project():
    svc, _ = service()
    project = svc.create("  v2 專案 ", " northwind/v2 ")
    assert (project.name, project.repo) == ("v2 專案", "northwind/v2")
    assert svc.get(project.id) == project
    assert svc.by_repo("northwind/v2") == [project]


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_name_is_rejected(name):
    svc, store = service()
    with pytest.raises(InvalidProjectOperation):
        svc.create(name)
    assert store.projects == {}


def test_duplicate_name_is_rejected_but_a_repo_may_be_shared():
    svc, store = service()
    first = svc.create("v2 專案", "northwind/v2")
    with pytest.raises(InvalidProjectOperation, match="name"):
        svc.create("v2 專案")
    second = svc.create("行動 App", "northwind/v2")  # DL-032: several projects may register one repo
    assert svc.by_repo("northwind/v2") == [first, second]


def test_blank_repo_means_no_repo_and_does_not_collide():
    svc, _ = service()
    assert svc.create("A", "").repo is None
    assert svc.create("B", "  ").repo is None


def test_list_counts_only_each_projects_open_alerts():
    svc, store = service()
    a, b = svc.create("A"), svc.create("B")
    with store.for_project(a.id).unit_of_work() as w:
        w.save_alert(alert("1"))
        w.save_alert(alert("2").model_copy(update={"status": "resolved"}))
    with store.for_project(b.id).unit_of_work() as w:
        w.save_alert(alert("3"))
        w.save_alert(alert("4"))
    assert {p.name: p.pending for p in svc.list()} == {"A": 1, "B": 2}


def test_unknown_project_is_not_found_and_unmapped_repo_is_none():
    svc, _ = service()
    with pytest.raises(NotFound):
        svc.get("P9")
    assert svc.by_repo("someone/else") == []



# --- DL-032: access control and repo registration ---

from decision_ledger.domain.errors import Forbidden, Unauthenticated  # noqa: E402
from decision_ledger.domain.models import Principal  # noqa: E402

ALICE, BOB = Principal(uid="alice"), Principal(uid="bob")


def guarded():
    store = FakeLedgerStore()
    showcase = store.create_project("Showcase", None)
    mine = store.create_project("Workspace a", None, project_id="wa", owner_uid="alice")
    legacy = store.create_project("Legacy", None)
    return ProjectService(store, showcase_id=showcase.id, auth_required=True), showcase, mine, legacy


@pytest.mark.parametrize("write", [False, True])
def test_owner_may_read_and_write_their_workspace(write):
    svc, _, mine, _ = guarded()
    assert svc.authorize(mine.id, ALICE, write=write) == mine


@pytest.mark.parametrize("write", [False, True])
def test_someone_elses_workspace_is_forbidden(write):
    svc, _, mine, legacy = guarded()
    for pid in (mine.id, legacy.id):
        with pytest.raises(Forbidden):
            svc.authorize(pid, BOB, write=write)


@pytest.mark.parametrize("write", [False, True])
def test_no_identity_is_unauthenticated_outside_the_showcase(write):
    svc, _, mine, _ = guarded()
    with pytest.raises(Unauthenticated):
        svc.authorize(mine.id, None, write=write)


def test_showcase_is_readable_by_anyone_and_writable_by_no_one():
    svc, showcase, _, _ = guarded()
    assert svc.authorize(showcase.id, None, write=False) == showcase
    assert svc.authorize(showcase.id, BOB, write=False) == showcase
    for who in (None, ALICE):
        with pytest.raises(Forbidden):
            svc.authorize(showcase.id, who, write=True)


def test_without_sign_in_every_project_is_open_except_showcase_writes():
    store = FakeLedgerStore()
    showcase, other = store.create_project("Showcase", None), store.create_project("Other", None)
    svc = ProjectService(store, showcase_id=showcase.id)
    assert svc.authorize(other.id, None, write=True) == other
    with pytest.raises(Forbidden):
        svc.authorize(showcase.id, None, write=True)


def test_unknown_project_is_not_found_before_any_access_decision():
    svc, *_ = guarded()
    with pytest.raises(NotFound):
        svc.authorize("nope", ALICE, write=False)


@pytest.mark.parametrize("repo,stored", [("acme/demo-repo", "acme/demo-repo"), ("  acme/x.y_z ", "acme/x.y_z"), ("", None), (None, None)])
def test_set_repo_validates_owner_name(repo, stored):
    svc, _, mine, _ = guarded()
    assert svc.set_repo(mine.id, repo).repo == stored
    assert svc.get(mine.id).repo == stored


@pytest.mark.parametrize("repo", ["acme", "acme/", "/x", "a/b/c", "acme demo/x", "https://github.com/a/b"])
def test_set_repo_rejects_other_shapes(repo):
    svc, _, mine, _ = guarded()
    with pytest.raises(InvalidProjectOperation):
        svc.set_repo(mine.id, repo)


def test_signed_in_mode_creates_projects_only_as_workspaces():
    svc, *_ = guarded()
    with pytest.raises(Forbidden):
        svc.create("Another")


# --- DL-032 review round 1 ---------------------------------------------------


def test_visible_lists_the_showcase_and_my_workspace_only():
    store = FakeLedgerStore()
    showcase = store.create_project("Showcase", None)
    mine = store.create_project("Mine", None, owner_uid="alice")
    store.create_project("Theirs", None, owner_uid="bob")
    svc = ProjectService(store, showcase_id=showcase.id, auth_required=True)
    assert [p.id for p in svc.visible(Principal(uid="alice"))] == [showcase.id, mine.id]
    assert [p.id for p in svc.visible(None)] == [showcase.id]
    assert [p.id for p in ProjectService(store).visible(None, showcase.id)] == [showcase.id]


def test_a_workspace_stays_closed_when_sign_in_is_off():
    """Review R1-07: if a server ever runs without sign-in, signed-in users' workspaces are not open to anyone."""
    store = FakeLedgerStore()
    owned = store.create_project("Workspace", None, owner_uid="alice")
    local = store.create_project("Local", None)
    svc = ProjectService(store, auth_required=False)
    with pytest.raises(Forbidden):
        svc.authorize(owned.id, None, write=False)
    assert svc.authorize(local.id, None, write=True) == local


def test_the_fake_store_refuses_to_update_an_unknown_project_like_the_real_ones():
    """Review R1-09: services tested on the fake see the same NotFound that SQLite and Firestore raise."""
    store = FakeLedgerStore()
    ghost = store.create_project("A", None).model_copy(update={"id": "P99"})
    with pytest.raises(NotFound):
        store.update_project(ghost)
    assert store.get_project("P99") is None



@pytest.mark.parametrize("repo", ["a" * 40 + "/x", "acme/" + "r" * 101, "acme/" + "r" * 600_000])
def test_repo_names_longer_than_github_allows_are_rejected(repo):
    """Review R3-01: an over-long repo is a 422, never a record too large to store."""
    svc, store = service()
    pid = svc.create("A").id
    with pytest.raises(InvalidProjectOperation):
        svc.set_repo(pid, repo)
    assert store.get_project(pid).repo is None
    assert svc.set_repo(pid, "a" * 39 + "/" + "r" * 100).repo


def test_project_names_are_capped():
    svc, store = service()
    with pytest.raises(InvalidProjectOperation):
        svc.create("n" * 201)
    assert svc.create("n" * 200).name == "n" * 200
