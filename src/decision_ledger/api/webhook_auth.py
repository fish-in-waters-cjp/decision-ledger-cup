"""GitHub webhook signature verification (transport-level HMAC, stdlib only)."""

from __future__ import annotations

import hashlib
import hmac


def sign(secret: str, body: bytes) -> str:
    """Return the ``X-Hub-Signature-256`` header value GitHub sends for ``body``."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str | None, body: bytes, header: str | None) -> bool:
    """True only when a secret is configured and the header matches; fails closed otherwise."""
    if not secret or not header:
        return False
    # Compare bytes: str comparison raises TypeError on non-ASCII input (headers arrive latin-1 decoded).
    return hmac.compare_digest(sign(secret, body).encode("utf-8"), header.strip().encode("utf-8"))
