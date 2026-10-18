"""DL-001: sandbox registry and usage counters (in-memory and Firestore via an in-memory client double)."""

import threading

import pytest

from decision_ledger.adapters.firestore_sandboxes import FirestoreSandboxRegistry, sandbox_doc_id
from decision_ledger.adapters.firestore_usage import FirestoreUsageCounter
from decision_ledger.adapters.memory import InMemorySandboxRegistry, InMemoryUsageCounter


class _Snap:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return dict(self._data) if self._data is not None else None

    def get(self, field):
        return (self._data or {}).get(field)


class _Doc:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def set(self, data, merge=False):
        current = dict(self.store.get(self.key) or {}) if merge else {}
        for k, v in data.items():
            current[k] = current.get(k, 0) + v[1] if isinstance(v, tuple) and v[0] == "inc" else v
        self.store[self.key] = current

    def get(self):
        return _Snap(self.store.get(self.key))

    def delete(self):
        self.store.pop(self.key, None)


class FakeFirestore:
    """Just enough of google.cloud.firestore.Client: collection(name).document(id).set/get/delete."""

    def __init__(self):
        self.data: dict[tuple[str, str], dict] = {}

    def collection(self, name):
        client = self

        class _Col:
            def document(self, doc_id):
                return _Doc(_Scoped(client.data, name), doc_id)

        return _Col()


class _Scoped(dict):
    def __init__(self, data, name):
        super().__init__()
        self.data, self.name = data, name

    def get(self, key, default=None):
        return self.data.get((self.name, key), default)

    def __setitem__(self, key, value):
        self.data[(self.name, key)] = value

    def pop(self, key, default=None):
        return self.data.pop((self.name, key), default)


def inc(n):
    return ("inc", n)


@pytest.fixture(params=["memory", "firestore"])
def counter(request):
    return InMemoryUsageCounter() if request.param == "memory" else FirestoreUsageCounter(FakeFirestore(), increment=inc)


@pytest.fixture(params=["memory", "firestore"])
def registry(request):
    return InMemorySandboxRegistry() if request.param == "memory" else FirestoreSandboxRegistry(FakeFirestore(), "salt", increment=inc)


def test_usage_counter_increments_per_scope_and_key(counter):
    assert counter.get("sandbox", "a:2026-10-17") == 0
    assert counter.increment("sandbox", "a:2026-10-17") == 1
    assert counter.increment("sandbox", "a:2026-10-17") == 2
    assert counter.increment("site", "2026-10-17") == 1
    assert counter.get("sandbox", "a:2026-10-17") == 2 and counter.get("sandbox", "b:2026-10-17") == 0


def test_registry_remembers_registered_ids(registry):
    assert registry.exists("abc") is False
    registry.register("abc")
    assert registry.exists("abc") is True and registry.exists("abd") is False


def test_firestore_registry_never_stores_the_raw_id():
    client = FakeFirestore()
    FirestoreSandboxRegistry(client, "salt", increment=inc).register("secret-sandbox-id")
    keys = [k for k in client.data if k[0] == "sandboxes"]
    assert keys == [("sandboxes", sandbox_doc_id("salt", "secret-sandbox-id"))]
    assert "secret-sandbox-id" not in repr(client.data)
    assert client.data[("counters", "sandboxes")]["count"] == 1
    assert sandbox_doc_id("salt", "x") != sandbox_doc_id("pepper", "x")


def test_firestore_usage_documents_live_in_usage_collection():
    client = FakeFirestore()
    FirestoreUsageCounter(client, increment=inc).increment("site", "2026-10-17")
    assert ("usage", "site:2026-10-17") in client.data


def test_firestore_registry_requires_a_salt():
    with pytest.raises(ValueError):
        FirestoreSandboxRegistry(FakeFirestore(), "", increment=inc)


def test_in_memory_counter_is_thread_safe():
    c = InMemoryUsageCounter()
    threads = [threading.Thread(target=lambda: [c.increment("s", "k") for _ in range(200)]) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.get("s", "k") == 1000


def test_default_increment_is_the_sdk_sentinel():
    firestore = pytest.importorskip("google.cloud.firestore")
    from decision_ledger.adapters.firestore_usage import firestore_increment

    assert isinstance(firestore_increment()(1), firestore.Increment)
    # Without an injected increment the adapters fall back to the SDK sentinel (no network needed).
    assert FirestoreUsageCounter(FakeFirestore())._increment is firestore.Increment
    assert FirestoreSandboxRegistry(FakeFirestore(), "salt")._increment is firestore.Increment
