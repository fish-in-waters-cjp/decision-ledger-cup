"""Core domain models for the Decision Ledger.

Every cross-layer value is one of these named Pydantic models. Decisions and
alerts must carry at least one piece of evidence (Constitution: no evidence,
no entry).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class SourceKind(StrEnum):
    TRANSCRIPT = "transcript"
    SLACK = "slack"


class Project(BaseModel):
    """A product the organization tracks; every ledger entity belongs to exactly one."""

    id: str
    name: str
    repo: str | None = None
    # Firebase uid of the signed-in user who owns this workspace; None for the showcase and local projects.
    owner_uid: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Principal(BaseModel):
    """The verified identity behind a request (Firebase uid)."""

    uid: str = Field(min_length=1)


class ProjectSummary(Project):
    """A project plus its number of open alerts, for the project switcher."""

    pending: int = 0


class Topic(BaseModel):
    """A subject decisions are grouped under; ``merged_into`` points at the topic that absorbed it."""

    id: str
    project_id: str = ""
    name: str
    merged_into: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Segment(BaseModel):
    """One addressable piece of a source: a transcript line or a Slack message."""

    index: int
    locator: str
    speaker: str
    text: str


class Source(BaseModel):
    id: str
    project_id: str = ""
    kind: SourceKind
    title: str
    occurred_on: str | None = None
    content_hash: str
    imported_at: datetime = Field(default_factory=utcnow)
    segments: list[Segment] = Field(default_factory=list)


class Evidence(BaseModel):
    """A verbatim quote pinned to a source location."""

    source_id: str
    locator: str
    speaker: str = ""
    quote: str = Field(min_length=1)


class Assumption(BaseModel):
    text: str
    evidence: list[Evidence] = Field(default_factory=list)


class CheckableRule(BaseModel):
    """A rule that can be checked against code changes."""

    description: str
    path_patterns: list[str] = Field(default_factory=list)


class DecisionStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    UNDER_REVIEW = "under_review"


class Decision(BaseModel):
    id: str
    project_id: str = ""
    topic_id: str
    # The subject as extracted from the source; display names come from ``Topic.name``.
    topic: str
    statement: str
    rationale: str
    decided_by: str
    decided_on: str | None = None
    assumptions: list[Assumption] = Field(default_factory=list)
    checkable_rule: CheckableRule | None = None
    status: DecisionStatus = DecisionStatus.ACTIVE
    superseded_by: str | None = None
    evidence: list[Evidence] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utcnow)


class AlertKind(StrEnum):
    SUPERSESSION = "supersession"
    CONFLICT = "conflict"
    ASSUMPTION = "assumption"
    DEVIATION = "deviation"


class AlertStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    OBSOLETE = "obsolete"


class ResolutionAction(StrEnum):
    CONFIRM_SUPERSESSION = "confirm_supersession"
    REJECT_SUPERSESSION = "reject_supersession"
    PREFER = "prefer"
    RECONSIDER = "reconsider"
    ASSUMPTION_HOLDS = "assumption_holds"
    MARK_EXCEPTION = "mark_exception"
    REAFFIRM = "reaffirm"


class Resolution(BaseModel):
    action: ResolutionAction
    prefer_decision_id: str | None = None
    note: str = ""


class Alert(BaseModel):
    id: str
    project_id: str = ""
    kind: AlertKind
    decision_ids: list[str] = Field(min_length=1)
    superseded_id: str | None = None
    superseding_id: str | None = None
    assumption_text: str | None = None
    location: str | None = None
    pr_check_key: str | None = None
    summary: str
    evidence: list[Evidence] = Field(min_length=1)
    status: AlertStatus = AlertStatus.OPEN
    resolution: Resolution | None = None
    created_at: datetime = Field(default_factory=utcnow)


class DecisionEvent(BaseModel):
    """An audit-log entry for anything that happened to a decision."""

    id: int | None = None
    decision_id: str
    kind: str
    alert_id: str | None = None
    note: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class DiscardedItem(BaseModel):
    kind: str
    summary: str
    reason: str


class ImportReport(BaseModel):
    source_id: str | None = None
    duplicate: bool = False
    segment_count: int = 0
    created: list[str] = Field(default_factory=list)
    merged: list[str] = Field(default_factory=list)
    supersession_alerts: list[str] = Field(default_factory=list)
    conflict_alerts: list[str] = Field(default_factory=list)
    assumption_alerts: list[str] = Field(default_factory=list)
    discarded: list[DiscardedItem] = Field(default_factory=list)
    topics_existing: int = 0
    topics_created: int = 0


class PullRequestDiff(BaseModel):
    """A unified diff to check; PR identity fields are optional for local checks."""

    repo: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    title: str = ""
    author: str = ""
    diff: str


class PullRequestEvent(BaseModel):
    """The subset of a GitHub pull_request webhook payload we act on."""

    action: str = ""
    repo: str
    pr_number: int
    head_sha: str
    title: str = ""
    author: str = ""
    diff_url: str | None = None
    diff: str | None = None


class PrCheck(BaseModel):
    """The stored outcome of checking one PR revision; keyed for idempotency."""

    key: str
    project_id: str = ""
    repo: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    title: str = ""
    author: str = ""
    diff: str
    comment: str
    violated: bool
    truncated: bool = False
    alert_ids: list[str] = Field(default_factory=list)
    comment_published: bool | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Health(StrEnum):
    HEALTHY = "healthy"
    CONFLICT = "conflict"
    DEVIATED = "deviated"
    AT_RISK = "at_risk"
    OUTDATED = "outdated"
    UNDER_REVIEW = "under_review"
    SUPERSEDED = "superseded"


class TopicHealth(StrEnum):
    """A topic's traffic light: conflict or deviation, doubtful premises or pending change, or stable."""

    ALERT = "alert"
    DOUBT = "doubt"
    STABLE = "stable"
