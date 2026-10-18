"""In-memory stand-in for the subset of google-cloud-firestore the adapters use (no network, no SDK).

Mirrors the real semantics that matter to the ledger: a ``WriteBatch`` applies all of its writes or none,
``create`` fails when the document exists, ``update`` with a ``last_update_time`` option fails when the
document changed since that time, and every write advances the document's ``update_time``.
"""

from __future__ import annotations

import copy
import itertools
import threading
from dataclasses import dataclass
from typing import Any


class FakeConflict(Exception):
    """Raised by ``create`` on an existing document (the SDK raises ``Conflict``/``AlreadyExists``)."""


DOCUMENT_LIMIT = 1_048_576


def _document_bytes(data: dict) -> int:
    """Roughly Firestore's size rule: field names plus UTF-8 string values (other values count their text)."""
    return sum(len(str(k).encode("utf-8")) + len(str(v).encode("utf-8")) + 1 for k, v in data.items()) + 32


class FakePreconditionFailed(Exception):
    """Raised when a ``last_update_time`` option no longer matches (the SDK raises ``FailedPrecondition``)."""


CONFLICT_ERRORS = (FakeConflict, FakePreconditionFailed)


@dataclass(frozen=True)
class FakeFieldFilter:
    field_path: str
    op_string: str
    value: Any


@dataclass(frozen=True)
class FakeLastUpdateOption:
    last_update_time: int


class FakeSnapshot:
    def __init__(self, reference: FakeDocumentReference, data: dict | None, update_time: int | None):
        self.reference = reference
        self.id = reference.id
        self._data = copy.deepcopy(data)
        self.exists = data is not None
        self.update_time = update_time

    def to_dict(self) -> dict | None:
        return copy.deepcopy(self._data)

    def get(self, field: str) -> Any:
        if self._data is None:
            raise KeyError(field)
        return copy.deepcopy(self._data.get(field))


class FakeDocumentReference:
    def __init__(self, client: FakeFirestoreClient, path: str):
        self._client = client
        self.path = path
        self.id = path.rsplit("/", 1)[-1]

    def get(self) -> FakeSnapshot:
        with self._client._lock:
            data, stamp = self._client._docs.get(self.path, (None, None))
            self._client.reads += 1
            return FakeSnapshot(self, data, stamp)

    def set(self, data: dict, merge: bool = False) -> None:
        batch = self._client.batch()
        batch.set(self, data, merge=merge)
        batch.commit()

    def delete(self) -> None:
        batch = self._client.batch()
        batch.delete(self)
        batch.commit()

    def collection(self, name: str) -> FakeCollectionReference:
        return FakeCollectionReference(self._client, f"{self.path}/{name}")


class FakeQuery:
    def __init__(self, collection: FakeCollectionReference, filters: tuple[FakeFieldFilter, ...]):
        self._collection = collection
        self._filters = filters

    def where(self, *, filter: FakeFieldFilter) -> FakeQuery:  # noqa: A002 - mirrors the SDK keyword
        return FakeQuery(self._collection, (*self._filters, filter))

    def stream(self):
        matched = [snap for snap in self._collection.snapshots() if all(_matches(snap, f) for f in self._filters)]
        with self._collection._client._lock:
            self._collection._client.reads += max(1, len(matched))  # billed per matching document
        yield from matched


def _matches(snap: FakeSnapshot, f: FakeFieldFilter) -> bool:
    if f.op_string != "==":
        raise NotImplementedError(f"fake supports only '==', got {f.op_string!r}")
    return (snap.to_dict() or {}).get(f.field_path) == f.value


class FakeCollectionReference:
    def __init__(self, client: FakeFirestoreClient, path: str):
        self._client = client
        self.path = path

    def document(self, doc_id: str) -> FakeDocumentReference:
        if "/" in doc_id or not doc_id:
            raise ValueError(f"invalid document id {doc_id!r}")
        return FakeDocumentReference(self._client, f"{self.path}/{doc_id}")

    def where(self, *, filter: FakeFieldFilter) -> FakeQuery:  # noqa: A002
        return FakeQuery(self, (filter,))

    def snapshots(self) -> list[FakeSnapshot]:
        """The collection's documents now, without counting reads (queries count only what they match)."""
        prefix = self.path + "/"
        with self._client._lock:
            items = [
                (path, data, stamp)
                for path, (data, stamp) in sorted(self._client._docs.items())
                if path.startswith(prefix) and "/" not in path[len(prefix):]
            ]
        return [FakeSnapshot(FakeDocumentReference(self._client, path), data, stamp) for path, data, stamp in items]

    def stream(self):
        snaps = self.snapshots()
        with self._client._lock:
            self._client.reads += max(1, len(snaps))  # Firestore bills one read per document (one for an empty query)
        yield from snaps


class FakeWriteBatch:
    def __init__(self, client: FakeFirestoreClient):
        self._client = client
        self._ops: list[tuple[str, FakeDocumentReference, dict | None, Any]] = []

    def set(self, reference: FakeDocumentReference, document_data: dict, merge: bool = False) -> None:
        self._ops.append(("merge" if merge else "set", reference, copy.deepcopy(document_data), None))

    def create(self, reference: FakeDocumentReference, document_data: dict) -> None:
        self._ops.append(("create", reference, copy.deepcopy(document_data), None))

    def update(self, reference: FakeDocumentReference, field_updates: dict, option: Any = None) -> None:
        self._ops.append(("update", reference, copy.deepcopy(field_updates), option))

    def delete(self, reference: FakeDocumentReference, option: Any = None) -> None:
        self._ops.append(("delete", reference, None, option))

    def __len__(self) -> int:
        return len(self._ops)

    def commit(self) -> None:
        client = self._client
        with client._lock:
            if client.fail_next_commit is not None:
                error, client.fail_next_commit = client.fail_next_commit, None
                raise error
            if len(self._ops) > 500:
                raise ValueError("a batch holds at most 500 writes")
            for _, ref, data, _ in self._ops:  # Firestore refuses a document over 1 MiB (InvalidArgument)
                if data is not None and _document_bytes(data) > DOCUMENT_LIMIT:
                    raise ValueError(f"{ref.path} exceeds the 1 MiB document limit")
            docs = client._docs
            for kind, ref, _, option in self._ops:  # check every precondition before applying anything
                current = docs.get(ref.path)
                if kind == "create" and current is not None:
                    raise FakeConflict(ref.path)
                if kind == "update" and current is None:
                    raise FakePreconditionFailed(f"{ref.path} does not exist")
                if isinstance(option, FakeLastUpdateOption) and (current is None or current[1] != option.last_update_time):
                    raise FakePreconditionFailed(f"{ref.path} changed")
            for kind, ref, data, _ in self._ops:
                stamp = next(client._clock)
                if kind == "delete":
                    docs.pop(ref.path, None)
                elif kind in ("update", "merge"):
                    merged = copy.deepcopy(docs.get(ref.path, ({}, None))[0])
                    merged.update(data)
                    docs[ref.path] = (merged, stamp)
                else:
                    docs[ref.path] = (data, stamp)
            client.commits += 1


class FakeFirestoreClient:
    """Thread-safe in-memory client; share one instance to simulate several Cloud Run instances."""

    def __init__(self):
        self._docs: dict[str, tuple[dict, int]] = {}
        self._clock = itertools.count(1)
        self._lock = threading.RLock()
        self.commits = 0
        self.reads = 0
        self.fail_next_commit: Exception | None = None

    def collection(self, name: str) -> FakeCollectionReference:
        return FakeCollectionReference(self, name)

    def batch(self) -> FakeWriteBatch:
        return FakeWriteBatch(self)

    @staticmethod
    def write_option(*, last_update_time: int) -> FakeLastUpdateOption:
        return FakeLastUpdateOption(last_update_time)

    def paths(self) -> list[str]:
        with self._lock:
            return sorted(self._docs)
