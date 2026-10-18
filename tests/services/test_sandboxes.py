"""DL-018: judge sandboxes are projects with unguessable ids."""

import re

import pytest

from decision_ledger.adapters.memory import InMemorySandboxRegistry
from decision_ledger.domain.errors import InvalidProjectOperation, NotFound, SandboxExpired
from decision_ledger.services.sandboxes import SandboxService, new_sandbox_id
from tests.fakes import FakeLedgerStore

ID = re.compile(r"^[A-Za-z0-9_-]{16}$")


def service(ids=None):
    store, registry, forgotten = FakeLedgerStore(), InMemorySandboxRegistry(), []
    showcase = store.create_project("Showcase", None)
    it = iter(ids) if ids else None
    svc = SandboxService(store, registry, forgotten.append, showcase_id=showcase.id,
                         new_id=(lambda: next(it)) if it else new_sandbox_id)
    return svc, store, registry, forgotten


def test_new_ids_are_16_url_safe_chars_and_not_sequential():
    ids = {new_sandbox_id() for _ in range(200)}
    assert len(ids) == 200 and all(ID.match(i) for i in ids)


def test_create_registers_a_project_whose_id_is_the_sandbox_id():
    svc, store, registry, _ = service()
    a, b = svc.create(), svc.create()
    assert ID.match(a.id) and ID.match(b.id) and a.id != b.id
    assert store.get_project(a.id) is not None and registry.exists(a.id) and registry.exists(b.id)


def test_colliding_ids_are_redrawn():
    svc, *_ = service(ids=["A" * 16, "A" * 16, "B" * 16])
    assert svc.create().id == "A" * 16
    assert svc.create().id == "B" * 16


def test_get_returns_live_sandbox_and_rejects_others():
    svc, store, registry, _ = service()
    sb = svc.create()
    assert svc.get(sb.id).id == sb.id
    with pytest.raises(NotFound):
        svc.get("P1")  # the showcase is not a sandbox
    with pytest.raises(NotFound):
        svc.get("x" * 16)


def test_registered_sandbox_whose_ledger_vanished_is_expired():
    svc, store, registry, _ = service()
    registry.register("gone" * 4)
    with pytest.raises(SandboxExpired) as err:
        svc.get("gone" * 4)
    assert "Sandbox expired, start a new one" in str(err.value)


def test_reset_clears_only_that_sandbox_and_drops_its_cache():
    svc, store, _, forgotten = service()
    a, b = svc.create(), svc.create()
    store.for_project(a.id).sources["s"] = object()
    store.for_project(b.id).sources["s"] = object()
    svc.reset(a.id)
    assert store.for_project(a.id).sources == {} and store.for_project(b.id).sources
    assert forgotten == [a.id]


def test_showcase_cannot_be_reset():
    svc, *_ = service()
    with pytest.raises((NotFound, InvalidProjectOperation)):
        svc.reset("P1")


# --- DL-018 follow-up: daily sandbox creation cap ------------------------------


def test_sandbox_creation_stops_at_the_daily_limit():
    from decision_ledger.adapters.memory import InMemoryUsageCounter
    from decision_ledger.domain.errors import SandboxLimitExceeded

    store, counter = FakeLedgerStore(), InMemoryUsageCounter()
    svc = SandboxService(store, InMemorySandboxRegistry(), lambda _: None, usage=counter, daily_create_limit=2,
                         today=lambda: "2026-10-18")
    svc.create(), svc.create()
    before = len(store.projects)
    with pytest.raises(SandboxLimitExceeded):
        svc.create()
    assert len(store.projects) == before
    assert counter.get("site-sandboxes", "2026-10-18") == 3


def test_sandbox_limit_resets_with_the_utc_day():
    from decision_ledger.adapters.memory import InMemoryUsageCounter

    day = ["2026-10-18"]
    svc = SandboxService(FakeLedgerStore(), InMemorySandboxRegistry(), lambda _: None, usage=InMemoryUsageCounter(),
                         daily_create_limit=1, today=lambda: day[0])
    svc.create()
    day[0] = "2026-10-19"
    assert svc.create().id


# --- DL-032: signed-in workspaces keyed by uid ---

from decision_ledger.adapters.memory import InMemoryUsageCounter  # noqa: E402
from decision_ledger.domain.errors import Forbidden, UserLimitExceeded  # noqa: E402
from decision_ledger.services.sandboxes import workspace_id  # noqa: E402
from tests.factories import decision  # noqa: E402


def signed_in(limit=100):
    store, registry, forgotten = FakeLedgerStore(), InMemorySandboxRegistry(), []
    showcase = store.create_project("Showcase", None)
    usage = InMemoryUsageCounter()
    svc = SandboxService(store, registry, forgotten.append, showcase_id=showcase.id, usage=usage,
                         auth_required=True, daily_new_user_limit=limit, today=lambda: "2026-10-17")
    return svc, store, registry, forgotten, usage


def test_workspace_id_is_stable_url_safe_and_reveals_nothing():
    assert workspace_id("uid-1") == workspace_id("uid-1") != workspace_id("uid-2")
    assert ID.match(workspace_id("uid-1")) and "uid" not in workspace_id("uid-1")


def test_first_sign_in_creates_an_owned_workspace_and_later_ones_return_it():
    svc, store, registry, _, _ = signed_in()
    first = svc.for_user("uid-1")
    assert first.owner_uid == "uid-1" and first.id == workspace_id("uid-1")
    assert svc.for_user("uid-1") == first and len(store.projects) == 2
    assert not registry.exists(first.id)  # workspaces are not anonymous sandboxes


def test_a_concurrent_first_sign_in_returns_the_workspace_the_other_request_created():
    svc, store, _, _, _ = signed_in()
    original = store.project_by_owner
    calls = {"n": 0}

    def racing(uid):
        calls["n"] += 1
        if calls["n"] == 1:  # the other tab creates it between our lookup and our create
            store.create_project("Workspace other tab", None, project_id=workspace_id(uid), owner_uid=uid)
            return None
        return original(uid)

    store.project_by_owner = racing
    project = svc.for_user("uid-1")
    assert project.id == workspace_id("uid-1") and len(store.projects) == 2


def test_new_users_over_the_daily_limit_are_refused_existing_users_are_not():
    svc, store, _, _, usage = signed_in(limit=1)
    svc.for_user("uid-1")
    with pytest.raises(UserLimitExceeded):
        svc.for_user("uid-2")
    assert store.project_by_owner("uid-2") is None
    assert svc.for_user("uid-1").owner_uid == "uid-1"
    assert usage.get("site-users", "2026-10-17") == 2


def test_reset_for_user_clears_only_their_workspace():
    svc, store, _, forgotten, _ = signed_in()
    mine, theirs = svc.for_user("uid-1"), svc.for_user("uid-2")
    for project in (mine, theirs):
        with store.for_project(project.id).unit_of_work() as w:
            w.save_decision(decision("1"))
    svc.reset_for_user("uid-1")
    assert store.for_project(mine.id).list_decisions() == [] and len(store.for_project(theirs.id).list_decisions()) == 1
    assert store.get_project(mine.id).owner_uid == "uid-1" and forgotten == [mine.id]


def test_reset_for_a_user_without_a_workspace_is_not_found():
    svc, *_ = signed_in()
    with pytest.raises(NotFound):
        svc.reset_for_user("nobody")


def test_signed_in_mode_turns_anonymous_sandboxes_off():
    svc, store, *_ = signed_in()
    for action in (svc.create, lambda: svc.get("abcdefghijklmnop"), lambda: svc.reset("abcdefghijklmnop")):
        with pytest.raises(Forbidden):
            action()
    assert len(store.projects) == 1
