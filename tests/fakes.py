"""In-memory test doubles for the domain ports (no adapters, no I/O)."""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel

from decision_ledger.domain.errors import DuplicatePrCheck, DuplicateSource, InvalidProjectOperation, NotFound
from decision_ledger.domain.models import (
    Alert,
    AlertStatus,
    Decision,
    DecisionEvent,
    DecisionStatus,
    Evidence,
    PrCheck,
    Project,
    ProjectSummary,
    PullRequestDiff,
    PullRequestEvent,
    Segment,
    Source,
    Topic,
)
from decision_ledger.domain.rules import normalize_topic


class FakeLLM:
    """Returns scripted responses.

    ``responses`` maps a key (exact) or a response-model name to either a value
    (dict / model) or a list of values consumed in order. A callable value is
    invoked with (prompt, key). Every call is recorded in ``calls``.
    """

    name = "fake"

    def __init__(self, responses: dict[str, Any] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, str, str]] = []

    def generate_json(self, prompt: str, response_model, *, key: str):
        self.calls.append((response_model.__name__, key, prompt))
        slot = key if key in self.responses else response_model.__name__
        if slot not in self.responses:
            return response_model()
        value = self.responses[slot]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, Exception):
            raise value
        if callable(value) and not isinstance(value, BaseModel):
            value = value(prompt, key)
        if isinstance(value, BaseModel):
            return value
        return response_model.model_validate(value)

    def keys_called(self, model_name: str | None = None) -> list[str]:
        return [k for m, k, _ in self.calls if model_name is None or m == model_name]


class _Writer:
    """Writes bound to the repository's project: every saved model is stamped with it."""

    def __init__(self, repo: FakeRepository):
        self.repo = repo

    def _stamp(self, model):
        return model.model_copy(update={"project_id": self.repo.project_id})

    def allocate_id(self, namespace: str) -> str:
        self.repo.counters[namespace] = self.repo.counters.get(namespace, 0) + 1
        return str(self.repo.counters[namespace])

    def save_source(self, source: Source) -> None:
        if any(s.content_hash == source.content_hash and s.id != source.id for s in self.repo.sources.values()):
            raise DuplicateSource(source.content_hash)
        self.repo.sources[source.id] = self._stamp(source)

    def save_decision(self, decision: Decision) -> None:
        self.repo.decisions[decision.id] = self._stamp(decision)

    def add_evidence(self, decision_id: str, evidence: Sequence[Evidence]) -> None:
        d = self.repo.decisions[decision_id]
        self.repo.decisions[decision_id] = d.model_copy(update={"evidence": [*d.evidence, *evidence]})

    def get_decision(self, decision_id: str) -> Decision | None:
        return self.repo.decisions.get(decision_id)

    def save_alert(self, alert: Alert) -> None:
        self.repo.alerts[alert.id] = self._stamp(alert)

    def get_alert_for_update(self, alert_id: str) -> Alert | None:
        return self.repo.alerts.get(alert_id)

    def obsolete_alerts_of(self, decision_ids: Sequence[str], except_alert_id: str | None) -> list[str]:
        changed = []
        for a in list(self.repo.alerts.values()):
            if a.status is AlertStatus.OPEN and a.id != except_alert_id and set(a.decision_ids) & set(decision_ids):
                self.repo.alerts[a.id] = a.model_copy(update={"status": AlertStatus.OBSOLETE})
                changed.append(a.id)
        return changed

    def append_event(self, event: DecisionEvent) -> None:
        self.repo.events.append(event.model_copy(update={"id": len(self.repo.events) + 1}))

    def save_pr_check(self, check: PrCheck) -> None:
        existing = self.repo.pr_checks.get(check.key)
        if existing is not None and existing.created_at != check.created_at:
            raise DuplicatePrCheck(check.key)
        self.repo.pr_checks[check.key] = self._stamp(check)

    def save_topic(self, topic: Topic) -> None:
        self.repo.topics[topic.id] = self._stamp(topic)

    def get_topic(self, topic_id: str) -> Topic | None:
        return self.repo.get_topic(topic_id)

    def resolve_topic(self, topic_id: str) -> Topic | None:
        return self.repo.resolve_topic(topic_id)

    def find_topic_by_name(self, name: str) -> Topic | None:
        return self.repo.find_topic_by_name(name)

    def list_topic_decisions(self, topic_id: str) -> list[Decision]:
        return [d for d in self.repo.decisions.values() if d.topic_id == topic_id]

    def rename_topic(self, topic_id: str, name: str) -> None:
        self.repo.topics[topic_id] = self.repo.topics[topic_id].model_copy(update={"name": name})

    def merge_topic(self, source_id: str, target_id: str) -> None:
        for d in list(self.repo.decisions.values()):
            if d.topic_id == source_id:
                self.repo.decisions[d.id] = d.model_copy(update={"topic_id": target_id})
        for t in list(self.repo.topics.values()):
            if t.id == source_id or t.merged_into == source_id:
                self.repo.topics[t.id] = t.model_copy(update={"merged_into": target_id})


class FakeRepository:
    """In-memory repository bound to one project; it has no way to reach another project."""

    def __init__(self, project_id: str = "P1", counters: dict[str, int] | None = None):
        self.project_id = project_id
        self.sources: dict[str, Source] = {}
        self.decisions: dict[str, Decision] = {}
        self.alerts: dict[str, Alert] = {}
        self.events: list[DecisionEvent] = []
        self.pr_checks: dict[str, PrCheck] = {}
        self.topics: dict[str, Topic] = {}
        # Ids are numbered per project, so every repository has its own counters.
        self.counters: dict[str, int] = {} if counters is None else counters
        self.commits = 0
        self.before_commit: Callable[[], None] | None = None

    _STATE = ("sources", "decisions", "alerts", "events", "pr_checks", "topics")

    @contextmanager
    def unit_of_work(self):
        snapshot = {name: copy.deepcopy(getattr(self, name)) for name in self._STATE}
        counters = dict(self.counters)
        try:
            yield _Writer(self)
            if self.before_commit:
                self.before_commit()
        except BaseException:
            for name, value in snapshot.items():
                setattr(self, name, value)
            self.counters.clear()
            self.counters.update(counters)
            raise
        self.commits += 1

    def find_source_by_hash(self, content_hash: str) -> Source | None:
        return next((s for s in self.sources.values() if s.content_hash == content_hash), None)

    def get_source(self, source_id: str) -> Source | None:
        return self.sources.get(source_id)

    def list_sources(self) -> list[Source]:
        return list(self.sources.values())

    def get_segments(self, source_id: str) -> list[Segment]:
        source = self.sources.get(source_id)
        return list(source.segments) if source else []

    def get_decision(self, decision_id: str) -> Decision | None:
        return self.decisions.get(decision_id)

    def list_decisions(self, statuses: Sequence[DecisionStatus] | None = None) -> list[Decision]:
        return [d for d in self.decisions.values() if statuses is None or d.status in statuses]

    def list_checkable_decisions(self) -> list[Decision]:
        return [d for d in self.decisions.values() if d.checkable_rule and d.status is DecisionStatus.ACTIVE]

    def search_decisions(self, terms: Sequence[str], limit: int) -> list[Decision]:
        scored = []
        for d in self.decisions.values():
            topic = self.topics.get(d.topic_id)
            haystack = f"{topic.name if topic else ''} {d.statement} {d.rationale}".lower()
            hits = sum(1 for t in terms if t.lower() in haystack)
            if hits:
                scored.append((hits, d))
        scored.sort(key=lambda x: -x[0])
        return [d for _, d in scored[:limit]]

    def get_alert(self, alert_id: str) -> Alert | None:
        return self.alerts.get(alert_id)

    def list_alerts(self, status: AlertStatus | None = None) -> list[Alert]:
        return [a for a in self.alerts.values() if status is None or a.status is status]

    def list_events(self, decision_id: str) -> list[DecisionEvent]:
        return [e for e in self.events if e.decision_id == decision_id]

    def get_pr_check(self, key: str) -> PrCheck | None:
        return self.pr_checks.get(key)

    def list_pr_checks(self) -> list[PrCheck]:
        return list(self.pr_checks.values())

    def list_topics(self, include_merged: bool = False) -> list[Topic]:
        return [t for t in self.topics.values() if include_merged or t.merged_into is None]

    def get_topic(self, topic_id: str) -> Topic | None:
        return self.topics.get(topic_id)

    def resolve_topic(self, topic_id: str) -> Topic | None:
        topic, seen = self.topics.get(topic_id), set()
        while topic is not None and topic.merged_into and topic.id not in seen:
            seen.add(topic.id)
            topic = self.topics.get(topic.merged_into)
        return topic

    def find_topic_by_name(self, name: str) -> Topic | None:
        key = normalize_topic(name)
        return next((t for t in self.list_topics() if normalize_topic(t.name) == key), None)


class FakeLedgerStore:
    """Unbound store: projects plus one bound FakeRepository per project, each numbering its own ids."""

    def __init__(self):
        self.projects: dict[str, Project] = {}
        self.repos: dict[str, FakeRepository] = {}
        self.counters: dict[str, int] = {}

    def for_project(self, project_id: str) -> FakeRepository:
        if project_id not in self.repos:
            self.repos[project_id] = FakeRepository(project_id)
        return self.repos[project_id]

    def create_project(
        self, name: str, repo: str | None, project_id: str | None = None, owner_uid: str | None = None
    ) -> Project:
        if project_id in self.projects or any(p.name == name for p in self.projects.values()):
            raise InvalidProjectOperation(f"project {project_id!r} or {name!r} already exists")
        if project_id is None:
            self.counters["project"] = self.counters.get("project", 0) + 1
        project = Project(id=project_id or f"P{self.counters['project']}", name=name, repo=repo, owner_uid=owner_uid)
        self.projects[project.id] = project
        return project

    def list_projects(self, ids=None) -> list[ProjectSummary]:
        return [
            ProjectSummary(
                **p.model_dump(),
                pending=len(self.for_project(p.id).list_alerts(AlertStatus.OPEN)),
            )
            for p in self.projects.values()
            if ids is None or p.id in ids
        ]

    def get_project(self, project_id: str) -> Project | None:
        return self.projects.get(project_id)

    def project_by_repo(self, repo: str) -> Project | None:
        return next(iter(self.projects_by_repo(repo)), None)

    def projects_by_repo(self, repo: str) -> list[Project]:
        return [p for p in self.projects.values() if p.repo == repo]

    def project_by_owner(self, owner_uid: str) -> Project | None:
        return next((p for p in self.projects.values() if p.owner_uid == owner_uid), None)

    def update_project(self, project: Project) -> None:
        if project.id not in self.projects:
            raise NotFound(f"project {project.id} not found")
        self.projects[project.id] = project

    def reset_project(self, project_id: str) -> None:
        self.repos.pop(project_id, None)


class FakePullRequestSource:
    def __init__(self, diff: str = ""):
        self.diff = diff
        self.calls: list[PullRequestEvent] = []

    def fetch_diff(self, event: PullRequestEvent) -> PullRequestDiff:
        self.calls.append(event)
        return PullRequestDiff(
            repo=event.repo, pr_number=event.pr_number, head_sha=event.head_sha,
            title=event.title, author=event.author, diff=event.diff or self.diff,
        )


class RecordingPublisher:
    def __init__(self, result: bool | None = None):
        self.result = result
        self.published: list[tuple[PullRequestDiff, str]] = []

    def publish(self, pr: PullRequestDiff, comment: str) -> bool | None:
        self.published.append((pr, comment))
        return self.result


class FakeUsageCounter:
    """In-memory ``UsageCounter``."""

    def __init__(self):
        self.counts: dict[tuple[str, str], int] = {}

    def increment(self, scope: str, key: str) -> int:
        self.counts[(scope, key)] = self.counts.get((scope, key), 0) + 1
        return self.counts[(scope, key)]

    def get(self, scope: str, key: str) -> int:
        return self.counts.get((scope, key), 0)
