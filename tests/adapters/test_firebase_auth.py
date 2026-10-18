"""DL-032: Firebase ID token verification and the fake verifier."""

from types import SimpleNamespace

import pytest

from decision_ledger.adapters.firebase_auth import CachingRequest, FakeIdentityVerifier, FirebaseIdentityVerifier
from decision_ledger.domain.errors import Unauthenticated

ISS = "https://securetoken.google.com/demo-proj"


def verifier(claims=None, error=None, calls=None):
    def verify(token, request, audience):
        if calls is not None:
            calls.append((token, audience))
        if error:
            raise error
        return claims

    return FirebaseIdentityVerifier("demo-proj", verify=verify, request=object())


def test_valid_token_yields_the_uid_and_uses_the_project_as_audience():
    calls = []
    principal = verifier({"iss": ISS, "sub": "uid-1", "aud": "demo-proj"}, calls=calls).verify("tok")
    assert principal.uid == "uid-1" and calls == [("tok", "demo-proj")]


@pytest.mark.parametrize("claims", [{"iss": "https://securetoken.google.com/other", "sub": "u"}, {"iss": ISS, "sub": ""}, None])
def test_wrong_issuer_or_missing_subject_is_unauthenticated(claims):
    with pytest.raises(Unauthenticated):
        verifier(claims).verify("tok")


def test_verification_errors_become_unauthenticated_without_echoing_the_token():
    with pytest.raises(Unauthenticated) as info:
        verifier(error=ValueError("Token expired, secret-token-123")).verify("secret-token-123")
    assert "secret-token-123" not in str(info.value)


def test_project_id_is_required():
    with pytest.raises(ValueError):
        FirebaseIdentityVerifier("")


def test_caching_request_keeps_certificates_for_max_age():
    now = {"t": 0.0}
    hits = []

    def inner(url, method="GET", **kwargs):
        hits.append(url)
        return SimpleNamespace(status=200, headers={"cache-control": "public, max-age=100"}, data=b"{}")

    request = CachingRequest(inner, clock=lambda: now["t"])
    request("https://certs")
    now["t"] = 99
    request("https://certs")
    assert hits == ["https://certs"]
    now["t"] = 101
    request("https://certs")
    assert hits == ["https://certs", "https://certs"]


def test_caching_request_does_not_cache_errors_or_uncacheable_responses():
    hits = []

    def inner(url, method="GET", **kwargs):
        hits.append(url)
        return SimpleNamespace(status=500 if url.endswith("err") else 200, headers={"cache-control": "max-age=60"} if url.endswith("err") else {}, data=b"")

    request = CachingRequest(inner, clock=lambda: 0.0)
    for url in ("https://err", "https://err", "https://nocache", "https://nocache"):
        request(url)
    assert len(hits) == 4


def test_fake_verifier():
    assert FakeIdentityVerifier().verify("fake:alice").uid == "alice"
    for token in ("alice", "fake:", ""):
        with pytest.raises(Unauthenticated):
            FakeIdentityVerifier().verify(token)
