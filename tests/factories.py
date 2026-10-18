"""Small builders for domain objects used across tests."""

from __future__ import annotations

from decision_ledger.domain.models import (
    Alert,
    AlertKind,
    Assumption,
    CheckableRule,
    Decision,
    DecisionStatus,
    Evidence,
    Project,
    Topic,
)


def ev(quote: str = "原文", source_id: str = "S1", locator: str = "00:01:00", speaker: str = "Alice") -> Evidence:
    return Evidence(source_id=source_id, locator=locator, speaker=speaker, quote=quote)


def decision(
    id: str = "1",
    *,
    topic: str = "v2 上線時程",
    topic_id: str = "T1",
    project_id: str = "P1",
    statement: str = "v2 在 10/15 上線",
    status: DecisionStatus = DecisionStatus.ACTIVE,
    assumptions: list[str] | None = None,
    rule: CheckableRule | None = None,
) -> Decision:
    return Decision(
        id=id,
        project_id=project_id,
        topic_id=topic_id,
        topic=topic,
        statement=statement,
        rationale="理由",
        decided_by="Alice",
        status=status,
        assumptions=[Assumption(text=a, evidence=[ev()]) for a in (assumptions or [])],
        checkable_rule=rule,
        evidence=[ev()],
    )


def alert(
    id: str = "1",
    kind: AlertKind = AlertKind.CONFLICT,
    decision_ids: list[str] | None = None,
    **kwargs,
) -> Alert:
    return Alert(
        id=id,
        kind=kind,
        project_id=kwargs.pop("project_id", "P1"),
        decision_ids=decision_ids or ["1"],
        summary="summary",
        evidence=kwargs.pop("evidence", None) or [ev()],
        **kwargs,
    )


def topic(id: str = "T1", name: str = "v2 上線時程", *, project_id: str = "P1", merged_into: str | None = None) -> Topic:
    return Topic(id=id, project_id=project_id, name=name, merged_into=merged_into)


def project(id: str = "P1", name: str = "v2 專案", repo: str | None = None) -> Project:
    return Project(id=id, name=name, repo=repo)
