"""Identity verification for signed-in users (DL-032): Firebase ID tokens, plus a fake for tests.

``FirebaseIdentityVerifier`` checks a Firebase ID token's signature, expiry, audience (the Firebase project id)
and issuer with google-auth, and returns the uid. Google's signing certificates rotate daily and are served with
``Cache-Control: max-age``; ``CachingRequest`` keeps them for that long, so a request costs a local RSA check, not
a network round trip. (google-auth's own transport does not cache, and the CacheControl package its docs suggest
is not a dependency here.) Tokens are never logged.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Any

from decision_ledger.domain.errors import Unauthenticated
from decision_ledger.domain.models import Principal

_MAX_AGE = re.compile(r"max-age=(\d+)")
FAKE_PREFIX = "fake:"


class CachingRequest:
    """A google-auth transport that caches successful GET responses for their ``Cache-Control: max-age``."""

    def __init__(self, inner: Callable[..., Any] | None = None, clock: Callable[[], float] = time.monotonic):
        self._inner = inner
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def _request(self) -> Callable[..., Any]:  # pragma: no cover - the default needs `requests`
        if self._inner is None:
            from google.auth.transport.requests import Request

            self._inner = Request()
        return self._inner

    def __call__(self, url: str, method: str = "GET", body: Any = None, headers: Any = None, timeout: Any = None, **kwargs: Any) -> Any:
        if method != "GET":
            return self._request()(url, method=method, body=body, headers=headers, timeout=timeout, **kwargs)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(url)
            if cached and cached[0] > now:
                return cached[1]
        response = self._request()(url, method=method, body=body, headers=headers, timeout=timeout, **kwargs)
        match = _MAX_AGE.search((getattr(response, "headers", {}) or {}).get("cache-control", "") or
                                (getattr(response, "headers", {}) or {}).get("Cache-Control", ""))
        if getattr(response, "status", None) == 200 and match:
            with self._lock:
                self._cache[url] = (now + int(match.group(1)), response)
        return response


def _verify_firebase_token(token: str, request: Any, audience: str) -> dict:  # pragma: no cover - needs google-auth
    from google.oauth2 import id_token

    return id_token.verify_firebase_token(token, request, audience=audience)


class FirebaseIdentityVerifier:
    """Implements ``IdentityVerifier`` for Firebase Authentication ID tokens."""

    def __init__(
        self,
        project_id: str,
        *,
        verify: Callable[[str, Any, str], dict] = _verify_firebase_token,
        request: Any = None,
    ):
        if not project_id:
            raise ValueError("a Firebase project id is required")
        self.project_id = project_id
        self._verify = verify
        self._request = request or CachingRequest()

    def verify(self, token: str) -> Principal:
        try:
            claims = self._verify(token, self._request, self.project_id)
        except Exception as exc:  # google-auth raises ValueError or transport errors; the token is never echoed
            raise Unauthenticated("invalid or expired sign-in token") from exc
        uid = (claims or {}).get("sub") or ""
        if (claims or {}).get("iss") != f"https://securetoken.google.com/{self.project_id}" or not uid:
            raise Unauthenticated("sign-in token was not issued for this app")
        return Principal(uid=uid)


class FakeIdentityVerifier:
    """Test-only verifier: ``fake:<uid>`` is the identity ``<uid>``. Refused in public mode by bootstrap."""

    def verify(self, token: str) -> Principal:
        uid = token[len(FAKE_PREFIX):] if token.startswith(FAKE_PREFIX) else ""
        if not uid:
            raise Unauthenticated("invalid or expired sign-in token")
        return Principal(uid=uid)
