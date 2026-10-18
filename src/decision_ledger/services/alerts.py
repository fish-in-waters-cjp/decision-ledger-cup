"""Apply a user's disposition of an alert — the only path that changes a decision's status."""

from __future__ import annotations

from decision_ledger.domain.errors import AlertAlreadyResolved, InvalidResolution, NotFound
from decision_ledger.domain.models import (
    Alert,
    AlertStatus,
    Decision,
    DecisionStatus,
    Resolution,
    ResolutionAction,
)
from decision_ledger.domain.ports import Repository
from decision_ledger.domain.rules import apply_transition


class AlertService:
    def __init__(self, repo: Repository):
        self.repo = repo

    def resolve(self, alert_id: str, resolution: Resolution) -> Alert:
        """Resolve an open alert; the status check and all writes share one transaction."""
        with self.repo.unit_of_work() as writer:
            alert = writer.get_alert_for_update(alert_id)
            if alert is None:
                raise NotFound(f"alert {alert_id} not found")
            if alert.status is not AlertStatus.OPEN:
                raise AlertAlreadyResolved(f"alert {alert_id} is {alert.status}")
            decisions = {i: d for i in alert.decision_ids if (d := writer.get_decision(i)) is not None}
            result = apply_transition(decisions, alert, resolution)

            for decision in result.decisions:
                writer.save_decision(decision)
            for event in result.events:
                writer.append_event(event)
            resolved = alert.model_copy(update={"status": AlertStatus.RESOLVED, "resolution": resolution})
            writer.save_alert(resolved)
            superseded = [d.id for d in result.decisions if d.status is DecisionStatus.SUPERSEDED]
            if superseded:
                writer.obsolete_alerts_of(superseded, except_alert_id=alert.id)
        return resolved

    def reaffirm(self, decision_id: str, note: str = "") -> Decision:
        """Return a decision under review to active."""
        with self.repo.unit_of_work() as writer:
            decision = writer.get_decision(decision_id)
            if decision is None:
                raise NotFound(f"decision {decision_id} not found")
            result = apply_transition({decision_id: decision}, None, Resolution(action=ResolutionAction.REAFFIRM, note=note))
            if not result.decisions:
                raise InvalidResolution(f"decision {decision_id} did not change")
            for changed in result.decisions:
                writer.save_decision(changed)
            for event in result.events:
                writer.append_event(event)
        return result.decisions[0]
