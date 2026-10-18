"""DL-021: the SQLite store doubles as the usage counter (atomic increments, isolated scopes)."""

from concurrent.futures import ThreadPoolExecutor

from decision_ledger.adapters.storage.sqlite import SqliteLedgerStore


def test_increment_and_get_per_scope_and_key():
    store = SqliteLedgerStore(":memory:")
    assert store.get("site", "2026-10-17") == 0
    assert store.increment("site", "2026-10-17") == 1
    assert store.increment("site", "2026-10-17") == 2
    assert store.increment("sandbox", "2026-10-17") == 1
    assert store.increment("site", "2026-10-18") == 1
    assert store.get("site", "2026-10-17") == 2


def test_usage_counts_do_not_disturb_project_numbering():
    store = SqliteLedgerStore(":memory:")
    for _ in range(3):
        store.increment("sandbox", "P1:d")
    assert store.create_project("first", None).id == "P1"


def test_concurrent_increments_are_not_lost(tmp_path):
    store = SqliteLedgerStore(str(tmp_path / "ledger.db"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.increment("site", "d"), range(200)))
    assert store.get("site", "d") == 200 and sorted(results) == list(range(1, 201))


def test_counts_survive_reopening_the_file(tmp_path):
    path = str(tmp_path / "ledger.db")
    SqliteLedgerStore(path).increment("sandbox", "P1:d")
    assert SqliteLedgerStore(path).get("sandbox", "P1:d") == 1
