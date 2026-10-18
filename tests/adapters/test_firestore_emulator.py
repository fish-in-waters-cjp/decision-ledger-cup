"""DL-032: the Firestore ledger against a real Firestore emulator (optional).

Runs only when ``FIRESTORE_EMULATOR_HOST`` is set (for example ``gcloud emulators firestore start
--host-port=127.0.0.1:8681``) and the ``cloud`` extra is installed; the default test run stays offline.
"""

import os
import uuid

import pytest

from tests.factories import decision

EMULATOR = os.environ.get("FIRESTORE_EMULATOR_HOST")
pytestmark = [
    pytest.mark.skipif(not EMULATOR, reason="FIRESTORE_EMULATOR_HOST is not set; start the Firestore emulator to run these"),
    pytest.mark.enable_socket,
]


@pytest.fixture
def client():
    firestore = pytest.importorskip("google.cloud.firestore", reason="the cloud extra is not installed")
    # A fresh logical project per test keeps runs independent on a shared emulator.
    return firestore.Client(project=f"dl-test-{uuid.uuid4().hex[:8]}")


def make(client):
    from decision_ledger.adapters.firestore_ledger import FirestoreLedgerStore

    return FirestoreLedgerStore(client)


def test_round_trip_and_redeploy(client):
    store = make(client)
    project = store.create_project("A", "acme/app", owner_uid="uid-1")
    with store.for_project(project.id).unit_of_work() as w:
        w.save_decision(decision(w.allocate_id("decision"), statement="report-service goes through the internal API"))
    again = make(client)
    assert again.project_by_owner("uid-1").id == project.id
    assert [d.id for d in again.for_project(project.id).list_decisions()] == ["1"]


def test_concurrent_commit_is_rejected(client):
    from decision_ledger.domain.errors import ConcurrentModification

    a, b = make(client), make(client)
    pid = a.create_project("A", None).id
    with b.for_project(pid).unit_of_work() as w:
        w.save_decision(decision("1"))
    with pytest.raises(ConcurrentModification):
        with a.for_project(pid).unit_of_work() as wa:
            wa.save_decision(decision("2"))
            with b.for_project(pid).unit_of_work() as wb:
                wb.save_decision(decision("3"))
