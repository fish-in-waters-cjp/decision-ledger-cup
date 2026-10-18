import pytest

from decision_ledger.domain.errors import AlertAlreadyResolved, InvalidResolution, NotFound
from decision_ledger.domain.models import AlertKind, AlertStatus, DecisionStatus, Resolution, ResolutionAction
from decision_ledger.services.alerts import AlertService
from tests.factories import alert, decision
from tests.fakes import FakeRepository

A = ResolutionAction


def repo_with(*alerts, decisions=("1", "2")):
    repo = FakeRepository()
    for i in decisions:
        repo.decisions[i] = decision(i)
    for a in alerts:
        repo.alerts[a.id] = a
    return repo


def test_confirm_supersession_resolves_alert_and_supersedes():
    a = alert("5", AlertKind.SUPERSESSION, ["1", "2"], superseded_id="1", superseding_id="2")
    repo = repo_with(a)
    resolved = AlertService(repo).resolve("5", Resolution(action=A.CONFIRM_SUPERSESSION, note="確認"))
    assert resolved.status is AlertStatus.RESOLVED and resolved.resolution.note == "確認"
    assert repo.alerts["5"].status is AlertStatus.RESOLVED
    assert repo.decisions["1"].status is DecisionStatus.SUPERSEDED and repo.decisions["1"].superseded_by == "2"
    assert [(e.decision_id, e.kind, e.alert_id) for e in repo.events] == [("1", "confirm_supersession", "5")]


def test_prefer_obsoletes_other_open_alerts_of_the_superseded_decision():
    conflict = alert("5", AlertKind.CONFLICT, ["1", "2"])
    other = alert("6", AlertKind.ASSUMPTION, ["2"])
    unrelated = alert("7", AlertKind.ASSUMPTION, ["1"])
    repo = repo_with(conflict, other, unrelated)
    AlertService(repo).resolve("5", Resolution(action=A.PREFER, prefer_decision_id="1"))
    assert repo.decisions["2"].status is DecisionStatus.SUPERSEDED
    assert repo.alerts["6"].status is AlertStatus.OBSOLETE
    assert repo.alerts["7"].status is AlertStatus.OPEN


@pytest.mark.parametrize("kind,action,status", [
    (AlertKind.ASSUMPTION, A.RECONSIDER, DecisionStatus.UNDER_REVIEW),
    (AlertKind.ASSUMPTION, A.ASSUMPTION_HOLDS, DecisionStatus.ACTIVE),
    (AlertKind.DEVIATION, A.MARK_EXCEPTION, DecisionStatus.ACTIVE),
    (AlertKind.DEVIATION, A.RECONSIDER, DecisionStatus.UNDER_REVIEW),
])
def test_single_decision_resolutions(kind, action, status):
    repo = repo_with(alert("5", kind, ["1"]))
    AlertService(repo).resolve("5", Resolution(action=action, note="n"))
    assert repo.decisions["1"].status is status
    assert repo.events[-1].kind == action.value and repo.events[-1].note == "n"


def test_resolving_twice_raises_already_resolved():
    repo = repo_with(alert("5", AlertKind.ASSUMPTION, ["1"]))
    svc = AlertService(repo)
    svc.resolve("5", Resolution(action=A.ASSUMPTION_HOLDS))
    with pytest.raises(AlertAlreadyResolved):
        svc.resolve("5", Resolution(action=A.ASSUMPTION_HOLDS))


def test_invalid_action_raises_and_changes_nothing():
    repo = repo_with(alert("5", AlertKind.ASSUMPTION, ["1"]))
    with pytest.raises(InvalidResolution):
        AlertService(repo).resolve("5", Resolution(action=A.MARK_EXCEPTION))
    assert repo.alerts["5"].status is AlertStatus.OPEN and repo.events == []


def test_unknown_alert_raises_not_found():
    with pytest.raises(NotFound):
        AlertService(repo_with()).resolve("404", Resolution(action=A.RECONSIDER))


def test_reaffirm_returns_decision_to_active():
    repo = repo_with()
    repo.decisions["1"] = decision("1", status=DecisionStatus.UNDER_REVIEW)
    result = AlertService(repo).reaffirm("1", note="仍然有效")
    assert result.status is DecisionStatus.ACTIVE
    assert repo.events[-1].kind == "reaffirm" and repo.events[-1].note == "仍然有效"


def test_reaffirm_rejects_active_and_unknown_decisions():
    svc = AlertService(repo_with())
    with pytest.raises(InvalidResolution):
        svc.reaffirm("1")
    with pytest.raises(NotFound):
        svc.reaffirm("404")


def test_resolution_stays_in_the_bound_project():
    a = alert("5", AlertKind.SUPERSESSION, ["1", "2"], superseded_id="1", superseding_id="2")
    repo = repo_with(a)
    AlertService(repo).resolve("5", Resolution(action=A.CONFIRM_SUPERSESSION))
    assert repo.alerts["5"].project_id == repo.decisions["1"].project_id == repo.project_id
