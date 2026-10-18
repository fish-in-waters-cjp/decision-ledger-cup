"""Ports: the only interfaces business logic depends on.

Concrete implementations (Gemini, Claude Code, Fake, SQLite, GitHub) live in
``adapters`` and are wired in ``bootstrap``.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from contextlib import AbstractContextManager
from typing import Protocol, TypeVar

from pydantic import BaseModel

from decision_ledger.domain.models import (
    Alert,
    AlertStatus,
    Decision,
    DecisionEvent,
    DecisionStatus,
    Evidence,
    PrCheck,
    Principal,
    Project,
    ProjectSummary,
    PullRequestDiff,
    PullRequestEvent,
    Segment,
    Source,
    Topic,
)

M = TypeVar("M", bound=BaseModel)


class LLMClient(Protocol):
    """A backend that answers a prompt with JSON matching ``response_model``.

    ``key`` deterministically identifies the request; real backends ignore it,
    the Fake backend looks up recorded responses by it.
    """

    name: str

    def generate_json(self, prompt: str, response_model: type[M], *, key: str) -> M: ...


class UsageCounter(Protocol):
    """Durable counters for usage limits; ``increment`` is atomic and returns the new count."""

    def increment(self, scope: str, key: str) -> int: ...

    def get(self, scope: str, key: str) -> int: ...


class IdentityVerifier(Protocol):
    """Turns a bearer token into a verified ``Principal``; raises ``Unauthenticated`` otherwise."""

    def verify(self, token: str) -> Principal: ...


class LedgerWriter(Protocol):
    """Write operations available inside one transaction, bound to one project.

    Every read is limited to the project and every write is stamped with it. Ids are unique only
    within the project: each project numbers its decisions, alerts, topics and sources from 1.
    """

    def allocate_id(self, namespace: str) -> str: ...

    def save_source(self, source: Source) -> None: ...

    def save_decision(self, decision: Decision) -> None: ...

    def add_evidence(self, decision_id: str, evidence: Sequence[Evidence]) -> None: ...

    def get_decision(self, decision_id: str) -> Decision | None: ...

    def save_alert(self, alert: Alert) -> None: ...

    def get_alert_for_update(self, alert_id: str) -> Alert | None: ...

    def obsolete_alerts_of(self, decision_ids: Sequence[str], except_alert_id: str | None) -> list[str]: ...

    def append_event(self, event: DecisionEvent) -> None: ...

    def save_pr_check(self, check: PrCheck) -> None: ...

    def save_topic(self, topic: Topic) -> None: ...

    def get_topic(self, topic_id: str) -> Topic | None: ...

    def resolve_topic(self, topic_id: str) -> Topic | None:
        """Follow ``merged_into`` to the topic that currently holds the decisions."""
        ...

    def find_topic_by_name(self, name: str) -> Topic | None:
        """The unmerged topic whose normalized name equals ``name``'s."""
        ...

    def list_topic_decisions(self, topic_id: str) -> list[Decision]:
        """The decisions currently in ``topic_id``, read inside the transaction."""
        ...

    def rename_topic(self, topic_id: str, name: str) -> None: ...

    def merge_topic(self, source_id: str, target_id: str) -> None:
        """Move ``source_id``'s decisions to ``target_id`` and repoint topics merged into the source."""
        ...


class Repository(Protocol):
    """Ledger storage bound to one project. Reads run outside transactions; writes go through ``unit_of_work``.

    There is deliberately no way to reach another project from here. Ids are unique only within the
    project, so an id never identifies anything outside it.
    """

    project_id: str

    def unit_of_work(self) -> AbstractContextManager[LedgerWriter]:
        """Open a transaction: commit on normal exit, roll back on any exception."""
        ...

    def find_source_by_hash(self, content_hash: str) -> Source | None: ...

    def get_source(self, source_id: str) -> Source | None: ...

    def list_sources(self) -> list[Source]: ...

    def get_segments(self, source_id: str) -> list[Segment]: ...

    def get_decision(self, decision_id: str) -> Decision | None: ...

    def list_decisions(self, statuses: Sequence[DecisionStatus] | None = None) -> list[Decision]: ...

    def list_checkable_decisions(self) -> list[Decision]: ...

    def search_decisions(self, terms: Sequence[str], limit: int) -> list[Decision]: ...

    def get_alert(self, alert_id: str) -> Alert | None: ...

    def list_alerts(self, status: AlertStatus | None = None) -> list[Alert]: ...

    def list_events(self, decision_id: str) -> list[DecisionEvent]: ...

    def get_pr_check(self, key: str) -> PrCheck | None: ...

    def list_pr_checks(self) -> list[PrCheck]: ...

    def list_topics(self, include_merged: bool = False) -> list[Topic]: ...

    def get_topic(self, topic_id: str) -> Topic | None: ...

    def resolve_topic(self, topic_id: str) -> Topic | None: ...

    def find_topic_by_name(self, name: str) -> Topic | None: ...


class LedgerStore(Protocol):
    """Unbound storage: the projects, and the way into one project's ``Repository``."""

    def for_project(self, project_id: str) -> Repository: ...

    def create_project(
        self, name: str, repo: str | None, project_id: str | None = None, owner_uid: str | None = None
    ) -> Project:
        """Raises InvalidProjectOperation when the id or name is already used; ``project_id`` defaults to the next ``P<n>``.

        A repo may be registered by several projects (DL-032).
        """
        ...

    def list_projects(self, ids: Collection[str] | None = None) -> list[ProjectSummary]:
        """Projects with their open-alert counts, oldest first; only those in ``ids`` when given (unknown ids skipped)."""
        ...

    def get_project(self, project_id: str) -> Project | None: ...

    def project_by_repo(self, repo: str) -> Project | None:
        """The oldest project registering ``repo`` (compatibility; prefer ``projects_by_repo``)."""
        ...

    def projects_by_repo(self, repo: str) -> list[Project]:
        """Every project registering ``repo``, oldest first."""
        ...

    def project_by_owner(self, owner_uid: str) -> Project | None:
        """The workspace owned by a signed-in user, if any."""
        ...

    def update_project(self, project: Project) -> None:
        """Replace the stored project fields (repo); an unknown id raises NotFound."""
        ...

    def reset_project(self, project_id: str) -> None:
        """Delete every row of the project (sources, decisions, alerts, topics, PR checks, id counters); keep the project."""
        ...


class SandboxRegistry(Protocol):
    """Which sandbox ids were ever handed out; kept outside the container so it survives restarts."""

    def register(self, sandbox_id: str) -> None: ...

    def exists(self, sandbox_id: str) -> bool: ...


class PullRequestSource(Protocol):
    """Resolves the diff for a pull_request webhook event."""

    def fetch_diff(self, event: PullRequestEvent) -> PullRequestDiff: ...


class CommentPublisher(Protocol):
    """Publishes a PR comment. Never sets commit status."""

    def publish(self, pr: PullRequestDiff, comment: str) -> bool | None:
        """Return True/False for a real publish attempt, None for a dry run."""
        ...
