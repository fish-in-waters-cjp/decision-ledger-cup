"""Decision state rules.

``apply_transition`` is the only place a Decision's status changes, and only
``AlertService`` calls it (Constitution: Human-in-the-loop — status changes
only after a user disposes of an alert). It is pure: it returns updated copies
and audit events and never touches storage.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from decision_ledger.domain.errors import InvalidResolution
from decision_ledger.domain.models import (
    Alert,
    AlertKind,
    Decision,
    DecisionEvent,
    DecisionStatus,
    Health,
    Resolution,
    ResolutionAction,
    TopicHealth,
)

A = ResolutionAction

ALLOWED_ACTIONS: dict[AlertKind | None, frozenset[ResolutionAction]] = {
    AlertKind.SUPERSESSION: frozenset({A.CONFIRM_SUPERSESSION, A.REJECT_SUPERSESSION}),
    AlertKind.CONFLICT: frozenset({A.PREFER, A.RECONSIDER}),
    AlertKind.ASSUMPTION: frozenset({A.ASSUMPTION_HOLDS, A.RECONSIDER}),
    AlertKind.DEVIATION: frozenset({A.MARK_EXCEPTION, A.RECONSIDER}),
    None: frozenset({A.REAFFIRM}),
}

_LIVE = {DecisionStatus.ACTIVE, DecisionStatus.UNDER_REVIEW}


@dataclass
class TransitionResult:
    """Decisions whose state changed, plus one audit event per affected decision."""

    decisions: list[Decision] = field(default_factory=list)
    events: list[DecisionEvent] = field(default_factory=list)


def apply_transition(
    decisions: Mapping[str, Decision],
    alert: Alert | None,
    resolution: Resolution,
) -> TransitionResult:
    """Apply a user's resolution to the decisions an alert refers to.

    ``alert`` is None only for ``reaffirm``, which acts on ``decisions`` directly.
    Raises InvalidResolution for any combination outside the transition table.
    """
    kind = alert.kind if alert else None
    action = resolution.action
    if action not in ALLOWED_ACTIONS[kind]:
        raise InvalidResolution(f"action {action} is not allowed for {kind or 'a decision'}")

    target_ids = alert.decision_ids if alert else list(decisions)
    missing = [i for i in target_ids if i not in decisions]
    if missing or not target_ids:
        raise InvalidResolution(f"unknown decision(s): {', '.join(missing) or '-'}")

    alert_id = alert.id if alert else None
    result = TransitionResult()

    def log(decision_id: str, event_kind: str) -> None:
        result.events.append(
            DecisionEvent(decision_id=decision_id, kind=event_kind, alert_id=alert_id, note=resolution.note)
        )

    def change(decision_id: str, status: DecisionStatus, superseded_by: str | None = None) -> None:
        current = decisions[decision_id]
        if current.status not in _LIVE:
            raise InvalidResolution(f"decision {decision_id} is {current.status}")
        result.decisions.append(current.model_copy(update={"status": status, "superseded_by": superseded_by}))
        log(decision_id, action.value)

    if action is A.CONFIRM_SUPERSESSION:
        old, new = _supersession_roles(alert)
        change(old, DecisionStatus.SUPERSEDED, superseded_by=new)
    elif action is A.REJECT_SUPERSESSION:
        for decision_id in target_ids:
            log(decision_id, action.value)
    elif action is A.PREFER:
        preferred = resolution.prefer_decision_id
        if preferred not in target_ids:
            raise InvalidResolution("prefer_decision_id must be one of the alert's decisions")
        for decision_id in target_ids:
            if decision_id != preferred:
                change(decision_id, DecisionStatus.SUPERSEDED, superseded_by=preferred)
        log(preferred, action.value)
    elif action is A.RECONSIDER:
        for decision_id in target_ids:
            change(decision_id, DecisionStatus.UNDER_REVIEW)
    elif action in (A.ASSUMPTION_HOLDS, A.MARK_EXCEPTION):
        for decision_id in target_ids:
            log(decision_id, action.value)
    elif action is A.REAFFIRM:
        for decision_id in target_ids:
            if decisions[decision_id].status is not DecisionStatus.UNDER_REVIEW:
                raise InvalidResolution(f"decision {decision_id} is not under review")
            change(decision_id, DecisionStatus.ACTIVE)
    return result


def _supersession_roles(alert: Alert | None) -> tuple[str, str]:
    if not alert or not alert.superseded_id or not alert.superseding_id:
        raise InvalidResolution("supersession alert is missing its decision roles")
    return alert.superseded_id, alert.superseding_id


def derive_health(decision: Decision, open_alerts: Sequence[Alert]) -> Health:
    """Derive the display health of a decision from its status and open alerts."""
    if decision.status is DecisionStatus.SUPERSEDED:
        return Health.SUPERSEDED
    if decision.status is DecisionStatus.UNDER_REVIEW:
        return Health.UNDER_REVIEW
    kinds = {a.kind for a in open_alerts if decision.id in a.decision_ids}
    if AlertKind.CONFLICT in kinds:
        return Health.CONFLICT
    if AlertKind.DEVIATION in kinds:
        return Health.DEVIATED
    if AlertKind.ASSUMPTION in kinds:
        return Health.AT_RISK
    if any(a.kind is AlertKind.SUPERSESSION and a.superseded_id == decision.id for a in open_alerts):
        return Health.OUTDATED
    return Health.HEALTHY


def normalize_topic(text: str) -> str:
    """Normalize width, case and whitespace so equal subjects compare equal."""
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def derive_topic_health(decisions: Sequence[Decision], open_alerts: Sequence[Alert]) -> TopicHealth:
    """Red for a pending conflict or deviation; yellow for a pending premise or supersession, or a decision under review."""
    ids = {d.id for d in decisions}
    kinds = {a.kind for a in open_alerts if ids & set(a.decision_ids)}
    if kinds & {AlertKind.CONFLICT, AlertKind.DEVIATION}:
        return TopicHealth.ALERT
    if kinds & {AlertKind.ASSUMPTION, AlertKind.SUPERSESSION} or any(
        d.status is DecisionStatus.UNDER_REVIEW for d in decisions
    ):
        return TopicHealth.DOUBT
    return TopicHealth.STABLE
