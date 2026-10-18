"""Firestore ``SandboxRegistry`` (DL-001, plan B).

Sandbox ids are bearer secrets (whoever has the id can use the sandbox), so the registry stores only
``HMAC-SHA256(salt, id)`` as the document id in ``sandboxes``; ``counters/sandboxes`` counts creations.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from decision_ledger.adapters.firestore_usage import firestore_increment


def sandbox_doc_id(salt: str, sandbox_id: str) -> str:
    """The Firestore document id for a sandbox id."""
    return hmac.new(salt.encode(), sandbox_id.encode(), hashlib.sha256).hexdigest()


class FirestoreSandboxRegistry:
    """Implements ``SandboxRegistry``."""

    def __init__(self, client: Any, salt: str, *, increment: Callable[[int], Any] | None = None):
        if not salt:
            raise ValueError("DL_SANDBOX_SALT is required for the Firestore sandbox registry")
        self._client, self._salt = client, salt
        self._increment = increment or firestore_increment()

    def register(self, sandbox_id: str) -> None:
        doc = self._client.collection("sandboxes").document(sandbox_doc_id(self._salt, sandbox_id))
        doc.set({"created_at": datetime.now(UTC).isoformat()})
        self._client.collection("counters").document("sandboxes").set({"count": self._increment(1)}, merge=True)

    def exists(self, sandbox_id: str) -> bool:
        return bool(self._client.collection("sandboxes").document(sandbox_doc_id(self._salt, sandbox_id)).get().exists)
