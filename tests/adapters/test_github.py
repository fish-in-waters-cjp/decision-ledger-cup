import json

import pytest

from decision_ledger.adapters.github import (
    DryRunCommentPublisher,
    GitHubCommentPublisher,
    GitHubPullRequestSource,
)
from decision_ledger.domain.errors import DiffUnavailable
from decision_ledger.domain.models import PullRequestDiff, PullRequestEvent


def event(**kw):
    base = dict(action="opened", repo="acme/app", pr_number=87, head_sha="abc", title="t", author="kevin")
    return PullRequestEvent(**{**base, **kw})


class Http:
    def __init__(self, body=b"", error=None):
        self.body, self.error, self.calls = body, error, []

    def __call__(self, method, url, headers, data=None, **kw):
        self.calls.append((method, url, headers, data))
        self.kwargs = kw
        if self.error:
            raise self.error
        return self.body


def test_inline_diff_in_payload_is_used_without_http():
    http = Http()
    pr = GitHubPullRequestSource(http=http).fetch_diff(event(diff="+x"))
    assert pr.diff == "+x" and pr.pr_number == 87 and pr.repo == "acme/app" and http.calls == []


def test_diff_url_is_fetched_with_token():
    http = Http(b"diff --git a/x b/x")
    pr = GitHubPullRequestSource(token="tkn", http=http).fetch_diff(event(diff_url="https://api.github.com/x.diff"))
    method, url, headers, _ = http.calls[0]
    assert method == "GET" and url == "https://api.github.com/x.diff"
    assert headers["Authorization"] == "Bearer tkn" and "diff" in headers["Accept"]
    assert pr.diff.startswith("diff --git")


def test_missing_diff_url_is_unavailable():
    with pytest.raises(DiffUnavailable):
        GitHubPullRequestSource(token="tkn", http=Http()).fetch_diff(event())


def test_without_token_a_public_diff_is_fetched_anonymously():
    """DL-003 follow-up: a public repo's diff_url needs no token."""
    http = Http(b"diff --git a/x b/x")
    pr = GitHubPullRequestSource(http=http).fetch_diff(event(diff_url="https://github.com/acme/app/pull/87.diff"))
    method, url, headers, _ = http.calls[0]
    assert method == "GET" and url.endswith("/pull/87.diff") and "Authorization" not in headers
    assert headers["Accept"] == "application/vnd.github.v3.diff" and http.kwargs == {"timeout": 8.0}
    assert pr.diff.startswith("diff --git")


def test_anonymous_404_is_unavailable():
    import urllib.error

    err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    with pytest.raises(DiffUnavailable, match="404"):
        GitHubPullRequestSource(http=Http(error=err)).fetch_diff(event(diff_url="https://github.com/acme/private/pull/1.diff"))


def test_fetch_failure_is_unavailable():
    with pytest.raises(DiffUnavailable):
        GitHubPullRequestSource(token="t", http=Http(error=OSError("down"))).fetch_diff(event(diff_url="u"))


def test_dry_run_publisher_returns_none():
    assert DryRunCommentPublisher().publish(PullRequestDiff(diff="d"), "c") is None


def test_github_publisher_posts_issue_comment_only():
    http = Http(b"{}")
    pr = PullRequestDiff(repo="acme/app", pr_number=87, diff="d")
    assert GitHubCommentPublisher("tkn", http=http).publish(pr, "留言") is True
    method, url, headers, data = http.calls[0]
    assert method == "POST" and url == "https://api.github.com/repos/acme/app/issues/87/comments"
    assert "/statuses/" not in url and "/check-runs" not in url
    assert json.loads(data) == {"body": "留言"} and headers["Authorization"] == "Bearer tkn"


def test_github_publisher_failure_returns_false_and_does_not_raise():
    pr = PullRequestDiff(repo="acme/app", pr_number=87, diff="d")
    assert GitHubCommentPublisher("t", http=Http(error=OSError("500"))).publish(pr, "c") is False


def test_github_publisher_without_pr_identity_returns_false():
    http = Http()
    assert GitHubCommentPublisher("t", http=http).publish(PullRequestDiff(diff="d"), "c") is False
    assert http.calls == []


def test_urllib_http_passes_request_details_and_returns_body(monkeypatch):
    """Constitution: every public function has a unit test (verify 3/5)."""
    import urllib.request

    from decision_ledger.adapters import github

    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"body"

    def fake_urlopen(request, timeout):
        seen.update(method=request.get_method(), url=request.full_url, headers=dict(request.header_items()),
                    data=request.data, timeout=timeout)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert github.urllib_http("POST", "https://api.github.com/x", {"Accept": "a"}, b"{}", timeout=3) == b"body"
    assert seen["method"] == "POST" and seen["url"] == "https://api.github.com/x"
    assert seen["headers"]["Accept"] == "a" and seen["data"] == b"{}" and seen["timeout"] == 3
