"""Read side of the ledger: natural-language Q&A and the views the UI renders."""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field

from decision_ledger.domain.errors import NotFound
from decision_ledger.domain.models import (
    Alert,
    AlertKind,
    AlertStatus,
    Decision,
    DecisionEvent,
    DecisionStatus,
    Evidence,
    Health,
    PrCheck,
    ResolutionAction,
    Source,
    Topic,
    TopicHealth,
)
from decision_ledger.domain.ports import LLMClient, Repository
from decision_ledger.domain.rules import derive_health, derive_topic_health
from decision_ledger.services.reconciliation import same_topic
from decision_ledger.services.structured import call_structured
from decision_ledger.services.drafts import decision_ref

NOT_FOUND_TEXT = "No related decision found"
SEARCH_LIMIT = 6

PROMPT = """Answer the question using ONLY the decisions listed below. Cite every decision you rely on by id (without '#').
If none of them answers the question, return an empty cited_decision_ids list. Answer in the language of the question.

QUESTION: {question}

DECISIONS:
{decisions}
"""

_ASCII = re.compile(r"[A-Za-z0-9_./-]{2,}")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
# Common Chinese question words, written as escapes so the source stays ASCII-only.
_STOP_BIGRAMS = {"\u70ba\u4ec0", "\u4ec0\u9ebc", "\u600e\u9ebc", "\u5982\u4f55", "\u662f\u5426", "\u54ea\u4e9b", "\u70ba\u4f55", "\u6211\u5011", "\u53ef\u4ee5", "\u9019\u500b", "\u90a3\u500b", "\u73fe\u5728", "\u8acb\u554f"}
# English function and question words; search matches substrings, so these would hit almost every decision.
_STOP_WORDS = {
    "a", "about", "an", "and", "any", "are", "as", "at", "be", "been", "can", "could", "did", "do", "does", "for",
    "from", "has", "have", "how", "in", "is", "it", "its", "of", "on", "or", "our", "should", "so", "that", "the",
    "there", "this", "to", "us", "was", "we", "were", "what", "when", "where", "which", "who", "why", "will", "with",
}
_HEALTH_ORDER = [
    Health.DEVIATED, Health.CONFLICT, Health.AT_RISK, Health.OUTDATED,
    Health.UNDER_REVIEW, Health.HEALTHY, Health.SUPERSEDED,
]
_TOPIC_HEALTH_ORDER = [TopicHealth.ALERT, TopicHealth.DOUBT, TopicHealth.STABLE]
_LIVE = (DecisionStatus.ACTIVE, DecisionStatus.UNDER_REVIEW)
MINI_TIMELINE_POINTS = 5


def terms(question: str) -> list[str]:
    """Search terms: ASCII words plus CJK bigrams, minus common question and function words."""
    found = [w for w in (w.lower() for w in _ASCII.findall(question)) if w not in _STOP_WORDS]
    for run in _CJK_RUN.findall(question):
        found += [run[i : i + 2] for i in range(len(run) - 1) if run[i : i + 2] not in _STOP_BIGRAMS]
    return list(dict.fromkeys(found))


class AnswerDraft(BaseModel):
    answer: str = ""
    cited_decision_ids: list[str] = Field(default_factory=list)


class Citation(BaseModel):
    decision_id: str
    statement: str
    status: DecisionStatus
    evidence: list[Evidence]


class Answer(BaseModel):
    question: str
    text: str
    found: bool
    citations: list[Citation] = Field(default_factory=list)


class QAService:
    def __init__(self, repo: Repository, llm: LLMClient):
        self.repo = repo
        self.llm = llm

    def answer(self, question: str) -> Answer:
        """Answer from ledger decisions only; never invent an answer without a cited decision."""
        question = question.strip()
        candidates = self.repo.search_decisions(terms(question), SEARCH_LIMIT)
        if not candidates:
            return Answer(question=question, text=NOT_FOUND_TEXT, found=False)
        topic_names = {t.id: t.name for t in self.repo.list_topics(include_merged=True)}
        draft = call_structured(
            self.llm,
            PROMPT.format(
                question=question,
                decisions="\n".join(
                    f"- #{d.id} [{d.status}] topic: {topic_names.get(d.topic_id, d.topic)}; {d.statement}; rationale: {d.rationale}; by {d.decided_by}"
                    + (f" on {d.decided_on}" if d.decided_on else "")
                    + (f"; superseded by #{d.superseded_by}" if d.superseded_by else "")
                    for d in candidates
                ),
            ),
            AnswerDraft,
            key="ask:" + hashlib.sha256(question.encode("utf-8")).hexdigest()[:16],
        )
        by_id = {d.id: d for d in candidates}
        cited = [by_id[i] for i in dict.fromkeys(decision_ref(c) for c in draft.cited_decision_ids) if i in by_id]
        if not cited or not draft.answer.strip():
            return Answer(question=question, text=NOT_FOUND_TEXT, found=False)
        return Answer(
            question=question,
            text=draft.answer.strip(),
            found=True,
            citations=[Citation(decision_id=d.id, statement=d.statement, status=d.status, evidence=d.evidence) for d in cited],
        )


class DecisionView(BaseModel):
    decision: Decision
    health: Health


class SourceSummary(BaseModel):
    id: str
    kind: str
    title: str
    occurred_on: str | None = None


class Overview(BaseModel):
    backend: str
    project: str
    sources: list[SourceSummary]
    health_counts: dict[Health, int]
    open_alerts: list[Alert]
    resolved_alerts: list[Alert]
    decisions: list[DecisionView]
    # Alert id → the topic its decisions belong to, for the topic chip on each alert.
    alert_topics: dict[str, Topic] = Field(default_factory=dict)


class TimelinePoint(BaseModel):
    decision_id: str
    decided_on: str | None = None
    status: DecisionStatus
    health: Health


class TopicCard(BaseModel):
    topic: Topic
    health: TopicHealth
    current: list[DecisionView]
    timeline: list[TimelinePoint]
    decision_count: int
    assumption_count: int
    source_count: int
    pending_count: int


class NodeChange(BaseModel):
    """How a decision changed its topic: added, updates #ref, contradicts #ref, or absorbed ``count`` sources."""

    kind: Literal["added", "updates", "conflicts", "merged_sources"]
    ref: str | None = None
    count: int = 0


class TimelineNode(BaseModel):
    decision: Decision
    health: Health
    changes: list[NodeChange]
    alerts: list[Alert]
    events: list[DecisionEvent]


class AssumptionView(BaseModel):
    text: str
    decision_id: str
    state: Literal["holds", "confirmed", "doubted", "overturned"]
    # For doubted / overturned premises: the new information and when it appeared.
    evidence: list[Evidence] = Field(default_factory=list)
    on: str | None = None


class TopicDetail(BaseModel):
    topic: Topic
    health: TopicHealth
    merged_into: Topic | None = None
    nodes: list[TimelineNode] = Field(default_factory=list)
    assumptions: list[AssumptionView] = Field(default_factory=list)


def _id_key(value: str) -> tuple[int, str]:
    return (int(value), value) if value.isdigit() else (1 << 30, value)


_FULL_DATE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_MONTH_DAY = re.compile(r"(\d{1,2})/(\d{1,2})")
_UNDATED = (1 << 30, 13, 32)


def _date_key(decision: Decision) -> tuple:
    """Order by ``decided_on`` (the first date in it: "2026-09-01", "9/1", "9/16–9/20"); undated last, then by id.

    A full date orders by year, month, day; a date without a year ("9/1") counts as year 0, so mixing the
    two forms in one topic puts the year-less ones first.
    """
    text = decision.decided_on or ""
    if m := _FULL_DATE.search(text):
        date = (int(m[1]), int(m[2]), int(m[3]))
    elif m := _MONTH_DAY.search(text):
        date = (0, int(m[1]), int(m[2]))
    else:
        date = _UNDATED
    return (date, _id_key(decision.id))


class LedgerQuery:
    def __init__(self, repo: Repository, backend: str, project: str = "Local Ledger"):
        self.repo = repo
        self.backend = backend
        self.project = project

    def _views(self, decisions: list[Decision]) -> list[DecisionView]:
        open_alerts = self.repo.list_alerts(AlertStatus.OPEN)
        views = [DecisionView(decision=d, health=derive_health(d, open_alerts)) for d in decisions]
        return sorted(views, key=lambda v: (_HEALTH_ORDER.index(v.health), _id_key(v.decision.id)))

    def overview(self) -> Overview:
        """Health counts, open and resolved alerts (each with its topic), and all decisions sorted by health."""
        decisions = self.repo.list_decisions()
        views = self._views(decisions)
        alerts = self.repo.list_alerts()
        return Overview(
            alert_topics=self._alert_topics(alerts, decisions),
            backend=self.backend,
            project=self.project,
            sources=[
                SourceSummary(id=s.id, kind=s.kind.value, title=s.title, occurred_on=s.occurred_on)
                for s in self.repo.list_sources()
            ],
            health_counts={h: sum(1 for v in views if v.health is h) for h in Health},
            open_alerts=sorted((a for a in alerts if a.status is AlertStatus.OPEN), key=lambda a: _id_key(a.id)),
            resolved_alerts=sorted((a for a in alerts if a.status is not AlertStatus.OPEN), key=lambda a: _id_key(a.id)),
            decisions=views,
        )

    def list(self) -> list[DecisionView]:
        """All decisions with their health, most urgent first."""
        return self._views(self.repo.list_decisions())

    def _alert_topics(self, alerts: list[Alert], decisions: list[Decision]) -> dict[str, Topic]:
        topic_of = {d.id: d.topic_id for d in decisions}
        found: dict[str, Topic] = {}
        for alert in alerts:
            topic_id = next((topic_of[i] for i in alert.decision_ids if i in topic_of), None)
            topic = self.repo.resolve_topic(topic_id) if topic_id else None
            if topic is not None:
                found[alert.id] = topic
        return found

    def topics(self) -> list[TopicCard]:
        """One card per live topic that holds decisions, red first, then yellow, then green."""
        decisions = self.repo.list_decisions()
        open_alerts = self.repo.list_alerts(AlertStatus.OPEN)
        cards = []
        for topic in self.repo.list_topics():
            members = sorted((d for d in decisions if d.topic_id == topic.id), key=_date_key)
            if not members:
                continue  # emptied by moves: hidden until a decision is assigned to it again
            ids = {d.id for d in members}
            health = {d.id: derive_health(d, open_alerts) for d in members}
            cards.append(
                TopicCard(
                    topic=topic,
                    health=derive_topic_health(members, open_alerts),
                    current=[DecisionView(decision=d, health=health[d.id]) for d in members if d.status in _LIVE],
                    timeline=[
                        TimelinePoint(decision_id=d.id, decided_on=d.decided_on, status=d.status, health=health[d.id])
                        for d in members[-MINI_TIMELINE_POINTS:]
                    ],
                    decision_count=len(members),
                    assumption_count=sum(len(d.assumptions) for d in members if d.status in _LIVE),
                    source_count=len({e.source_id for d in members for e in d.evidence}),
                    pending_count=sum(1 for a in open_alerts if ids & set(a.decision_ids)),
                )
            )
        return sorted(cards, key=lambda c: (_TOPIC_HEALTH_ORDER.index(c.health), _id_key(c.topic.id)))

    def topic_detail(self, topic_id: str) -> TopicDetail:
        """A topic's decisions in order with their changes, alerts and premises; a merged topic names its target."""
        topic = self.repo.get_topic(topic_id)
        if topic is None:
            raise NotFound(f"topic {topic_id} not found")
        if topic.merged_into is not None:
            target = self.repo.resolve_topic(topic.id)
            return TopicDetail(topic=topic, health=TopicHealth.STABLE, merged_into=target)

        members = sorted((d for d in self.repo.list_decisions() if d.topic_id == topic.id), key=_date_key)
        ids = {d.id for d in members}
        alerts = [a for a in self.repo.list_alerts() if ids & set(a.decision_ids) and a.status is not AlertStatus.OBSOLETE]
        open_alerts = [a for a in alerts if a.status is AlertStatus.OPEN]
        nodes = []
        for d in members:
            events = self.repo.list_events(d.id)
            nodes.append(
                TimelineNode(
                    decision=d,
                    health=derive_health(d, open_alerts),
                    changes=_changes(d, alerts, events),
                    alerts=sorted((a for a in alerts if _anchor(a) == d.id), key=lambda a: _id_key(a.id)),
                    events=events,
                )
            )
        return TopicDetail(
            topic=topic,
            health=derive_topic_health(members, open_alerts),
            nodes=nodes,
            assumptions=[self._assumption_view(d, a.text, alerts) for d in members for a in d.assumptions],
        )

    def _assumption_view(self, decision: Decision, text: str, alerts: list[Alert]) -> AssumptionView:
        alert = next(
            (
                a
                for a in reversed(alerts)
                if a.kind is AlertKind.ASSUMPTION and decision.id in a.decision_ids and same_topic(a.assumption_text or "", text)
            ),
            None,
        )
        if alert is None:
            return AssumptionView(text=text, decision_id=decision.id, state="holds")
        if alert.status is AlertStatus.RESOLVED and alert.resolution is not None:
            if alert.resolution.action is ResolutionAction.ASSUMPTION_HOLDS:
                return AssumptionView(text=text, decision_id=decision.id, state="confirmed")
            state = "overturned"
        else:
            state = "doubted"
        new_info = [e for e in alert.evidence if e not in decision.evidence and not any(e in a.evidence for a in decision.assumptions)]
        source = self.repo.get_source(new_info[0].source_id) if new_info else None
        return AssumptionView(
            text=text, decision_id=decision.id, state=state, evidence=new_info, on=source.occurred_on if source else None
        )

    def alerts(self, status: AlertStatus | None = None) -> list[Alert]:
        """Alerts, optionally filtered by status."""
        return sorted(self.repo.list_alerts(status), key=lambda a: _id_key(a.id))

    def pr_checks(self) -> list[PrCheck]:
        """Stored PR checks, newest first."""
        return sorted(self.repo.list_pr_checks(), key=lambda c: c.created_at, reverse=True)

    def source(self, source_id: str) -> Source:
        """A source with its segments, for showing evidence in context."""
        source = self.repo.get_source(source_id)
        if source is None:
            raise NotFound(f"source {source_id} not found")
        return source


def _anchor(alert: Alert) -> str:
    """The timeline node an alert is shown next to: the replaced decision, the newer side of a conflict, else its decision."""
    if alert.kind is AlertKind.SUPERSESSION and alert.superseded_id:
        return alert.superseded_id
    return alert.decision_ids[-1]


def _changes(decision: Decision, alerts: list[Alert], events: list[DecisionEvent]) -> list[NodeChange]:
    changes = [
        NodeChange(kind="updates", ref=a.superseded_id)
        for a in alerts
        if a.kind is AlertKind.SUPERSESSION and a.superseding_id == decision.id
    ]
    changes += [
        NodeChange(kind="conflicts", ref=a.decision_ids[0])
        for a in alerts
        if a.kind is AlertKind.CONFLICT and a.decision_ids[-1] == decision.id and len(a.decision_ids) > 1
    ]
    if not changes:
        changes.append(NodeChange(kind="added"))
    merged = sum(1 for e in events if e.kind == "merged")
    if merged:
        changes.append(NodeChange(kind="merged_sources", count=merged))
    return changes
