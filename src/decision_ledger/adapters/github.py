"""GitHub integration: resolve PR diffs and publish advisory comments.

Only the issue-comment endpoint is ever called; commit statuses and check runs
are never set, so a comment can never block a merge.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Callable

from decision_ledger.domain.errors import DiffUnavailable
from decision_ledger.domain.models import PullRequestDiff, PullRequestEvent

log = logging.getLogger(__name__)

API = "https://api.github.com"
HttpCall = Callable[[str, str, dict[str, str], bytes | None], bytes]
HTTP_TIMEOUT = 15.0
ANONYMOUS_TIMEOUT = 8.0
DIFF_ACCEPT = "application/vnd.github.v3.diff"


def urllib_http(
    method: str, url: str, headers: dict[str, str], data: bytes | None = None, timeout: float = HTTP_TIMEOUT
) -> bytes:
    """Minimal HTTP call with the standard library."""
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL comes from GitHub/config
        return response.read()


def _headers(token: str, accept: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}


class GitHubPullRequestSource:
    """Uses a diff embedded in the payload (local demo), else downloads ``diff_url``: with the token when one is
    set, otherwise anonymously (public repositories; a private one answers 404 and the diff is unavailable)."""

    def __init__(self, token: str | None = None, http: HttpCall = urllib_http):
        self.token = token
        self.http = http

    def fetch_diff(self, event: PullRequestEvent) -> PullRequestDiff:
        diff = event.diff
        if diff is None:
            if not event.diff_url:
                raise DiffUnavailable("payload has no diff and no diff_url to fetch it")
            try:
                if self.token:
                    raw = self.http("GET", event.diff_url, _headers(self.token, DIFF_ACCEPT), None)
                else:
                    raw = self.http("GET", event.diff_url, {"Accept": DIFF_ACCEPT}, None, timeout=ANONYMOUS_TIMEOUT)
                diff = raw.decode("utf-8")
            except Exception as exc:  # 401/404 for a private repo without a token, network errors, timeouts
                hint = "" if self.token else " (no DL_GITHUB_TOKEN: only public repositories can be read)"
                raise DiffUnavailable(f"could not download PR diff: {exc}{hint}") from exc
        return PullRequestDiff(
            repo=event.repo, pr_number=event.pr_number, head_sha=event.head_sha,
            title=event.title, author=event.author, diff=diff,
        )


class DryRunCommentPublisher:
    """Default publisher: the comment is only returned by the API, never posted."""

    def publish(self, pr: PullRequestDiff, comment: str) -> bool | None:
        return None


class GitHubCommentPublisher:
    """Posts the comment on the PR conversation; failures are logged, never raised."""

    def __init__(self, token: str, http: HttpCall = urllib_http):
        self.token = token
        self.http = http

    def publish(self, pr: PullRequestDiff, comment: str) -> bool | None:
        if not pr.repo or pr.pr_number is None:
            return False
        url = f"{API}/repos/{pr.repo}/issues/{pr.pr_number}/comments"
        try:
            self.http("POST", url, _headers(self.token, "application/vnd.github+json"), json.dumps({"body": comment}).encode("utf-8"))
        except Exception as exc:
            log.warning("publishing PR comment failed for %s#%s: %s", pr.repo, pr.pr_number, exc)
            return False
        return True
