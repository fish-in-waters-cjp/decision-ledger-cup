"""Firestore ``UsageCounter``: daily LLM usage that survives Cloud Run restarts (DL-001, plan B).

Documents live in the ``usage`` collection with id ``{scope}:{key}`` and a ``count`` field.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


def firestore_increment() -> Callable[[int], Any]:
    """The SDK's atomic increment sentinel (imported lazily; the SDK is the optional ``cloud`` extra)."""
    from google.cloud import firestore

    return firestore.Increment


class FirestoreUsageCounter:
    """Implements ``UsageCounter``.

    ``increment`` applies an atomic server-side increment, then reads the document back. Under concurrent
    increments two callers may read the same final value; for limit checks that errs on the strict side.
    """

    def __init__(self, client: Any, *, increment: Callable[[int], Any] | None = None, collection: str = "usage"):
        self._client = client
        self._increment = increment or firestore_increment()
        self._collection = collection

    def _doc(self, scope: str, key: str) -> Any:
        return self._client.collection(self._collection).document(f"{scope}:{key}")

    def increment(self, scope: str, key: str) -> int:
        doc = self._doc(scope, key)
        doc.set(
            {"count": self._increment(1), "scope": scope, "key": key, "updated_at": datetime.now(UTC).isoformat()},
            merge=True,
        )
        return int(doc.get().get("count") or 0)

    def get(self, scope: str, key: str) -> int:
        snap = self._doc(scope, key).get()
        return int(snap.get("count") or 0) if snap.exists else 0
