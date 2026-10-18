import pytest

from decision_ledger.domain.errors import InvalidResolution
from decision_ledger.domain.models import (
    AlertKind,
    DecisionStatus,
    Health,
    Resolution,
    ResolutionAction,
    TopicHealth,
)
from decision_ledger.domain.rules import apply_transition, derive_health, derive_topic_health, normalize_topic
from decision_ledger.services.reconciliation import same_topic
from tests.factories import alert, decision

A = ResolutionAction
ACTIVE, SUPERSEDED, REVIEW = DecisionStatus.ACTIVE, DecisionStatus.SUPERSEDED, DecisionStatus.UNDER_REVIEW


def by_id(result):
    return {d.id: d for d in result.decisions}


def test_confirm_supersession_supersedes_old_decision():
    old, new = decision("1"), decision("2", statement="v2 延到 11/1")
    a = alert(kind=AlertKind.SUPERSESSION, decision_ids=["1", "2"], superseded_id="1", superseding_id="2")
    result = apply_transition({"1": old, "2": new}, a, Resolution(action=A.CONFIRM_SUPERSESSION))
    changed = by_id(result)
    assert changed["1"].status == SUPERSEDED
    assert changed["1"].superseded_by == "2"
    assert "2" not in changed
    assert [e.decision_id for e in result.events] == ["1"]
    assert result.events[0].alert_id == a.id


def test_confirm_supersession_from_under_review():
    old = decision("1", status=REVIEW)
    a = alert(kind=AlertKind.SUPERSESSION, decision_ids=["1", "2"], superseded_id="1", superseding_id="2")
    result = apply_transition({"1": old, "2": decision("2")}, a, Resolution(action=A.CONFIRM_SUPERSESSION))
    assert by_id(result)["1"].status == SUPERSEDED


def test_reject_supersession_changes_nothing_but_logs_both():
    a = alert(kind=AlertKind.SUPERSESSION, decision_ids=["1", "2"], superseded_id="1", superseding_id="2")
    result = apply_transition({"1": decision("1"), "2": decision("2")}, a, Resolution(action=A.REJECT_SUPERSESSION))
    assert result.decisions == []
    assert sorted(e.decision_id for e in result.events) == ["1", "2"]


def test_prefer_supersedes_the_other_decision():
    a = alert(kind=AlertKind.CONFLICT, decision_ids=["1", "2"])
    result = apply_transition(
        {"1": decision("1"), "2": decision("2")}, a, Resolution(action=A.PREFER, prefer_decision_id="1")
    )
    changed = by_id(result)
    assert changed["2"].status == SUPERSEDED and changed["2"].superseded_by == "1"
    assert "1" not in changed


def test_prefer_requires_a_member_decision():
    a = alert(kind=AlertKind.CONFLICT, decision_ids=["1", "2"])
    with pytest.raises(InvalidResolution):
        apply_transition({"1": decision("1"), "2": decision("2")}, a, Resolution(action=A.PREFER, prefer_decision_id="9"))


def test_conflict_reconsider_puts_both_under_review():
    a = alert(kind=AlertKind.CONFLICT, decision_ids=["1", "2"])
    result = apply_transition({"1": decision("1"), "2": decision("2")}, a, Resolution(action=A.RECONSIDER))
    assert {d.status for d in result.decisions} == {REVIEW}
    assert len(result.decisions) == 2


@pytest.mark.parametrize("kind", [AlertKind.ASSUMPTION, AlertKind.DEVIATION])
def test_reconsider_puts_decision_under_review(kind):
    result = apply_transition({"1": decision("1")}, alert(kind=kind), Resolution(action=A.RECONSIDER))
    assert by_id(result)["1"].status == REVIEW


def test_assumption_holds_keeps_decision_active_and_logs():
    result = apply_transition(
        {"1": decision("1")}, alert(kind=AlertKind.ASSUMPTION), Resolution(action=A.ASSUMPTION_HOLDS, note="新人還在 onboarding")
    )
    assert result.decisions == []
    assert result.events[0].kind == "assumption_holds"
    assert result.events[0].note == "新人還在 onboarding"


def test_mark_exception_keeps_decision_active_and_logs_note():
    result = apply_transition(
        {"1": decision("1")}, alert(kind=AlertKind.DEVIATION), Resolution(action=A.MARK_EXCEPTION, note="一次性遷移")
    )
    assert result.decisions == []
    assert result.events[0].kind == "mark_exception"


def test_reaffirm_returns_under_review_to_active():
    result = apply_transition({"1": decision("1", status=REVIEW)}, None, Resolution(action=A.REAFFIRM))
    assert by_id(result)["1"].status == ACTIVE


def test_reaffirm_requires_under_review():
    with pytest.raises(InvalidResolution):
        apply_transition({"1": decision("1")}, None, Resolution(action=A.REAFFIRM))


ALLOWED = {
    AlertKind.SUPERSESSION: {A.CONFIRM_SUPERSESSION, A.REJECT_SUPERSESSION},
    AlertKind.CONFLICT: {A.PREFER, A.RECONSIDER},
    AlertKind.ASSUMPTION: {A.ASSUMPTION_HOLDS, A.RECONSIDER},
    AlertKind.DEVIATION: {A.MARK_EXCEPTION, A.RECONSIDER},
}


@pytest.mark.parametrize(
    "kind,action",
    [(k, a) for k in AlertKind for a in ResolutionAction if a not in ALLOWED[k]],
)
def test_actions_outside_the_table_are_rejected(kind, action):
    a = alert(kind=kind, decision_ids=["1", "2"], superseded_id="1", superseding_id="2")
    with pytest.raises(InvalidResolution):
        apply_transition({"1": decision("1"), "2": decision("2")}, a, Resolution(action=action, prefer_decision_id="1"))


def test_alert_actions_without_alert_are_rejected():
    with pytest.raises(InvalidResolution):
        apply_transition({"1": decision("1")}, None, Resolution(action=A.RECONSIDER))


def test_superseded_decision_cannot_be_reconsidered():
    with pytest.raises(InvalidResolution):
        apply_transition({"1": decision("1", status=SUPERSEDED)}, alert(kind=AlertKind.ASSUMPTION), Resolution(action=A.RECONSIDER))


def test_missing_decision_is_rejected():
    with pytest.raises(InvalidResolution):
        apply_transition({}, alert(kind=AlertKind.ASSUMPTION), Resolution(action=A.RECONSIDER))


def test_transition_does_not_mutate_inputs():
    original = decision("1")
    apply_transition({"1": original}, alert(kind=AlertKind.ASSUMPTION), Resolution(action=A.RECONSIDER))
    assert original.status == ACTIVE


# --- derive_health -------------------------------------------------------


def test_health_status_overrides_alerts():
    open_conflict = [alert(kind=AlertKind.CONFLICT)]
    assert derive_health(decision("1", status=SUPERSEDED), open_conflict) == Health.SUPERSEDED
    assert derive_health(decision("1", status=REVIEW), open_conflict) == Health.UNDER_REVIEW


def test_health_priority_conflict_then_deviation_then_assumption():
    d = decision("1")
    kinds = [AlertKind.ASSUMPTION, AlertKind.DEVIATION, AlertKind.CONFLICT]
    assert derive_health(d, [alert(str(i), k) for i, k in enumerate(kinds)]) == Health.CONFLICT
    assert derive_health(d, [alert("1", AlertKind.ASSUMPTION), alert("2", AlertKind.DEVIATION)]) == Health.DEVIATED
    assert derive_health(d, [alert("1", AlertKind.ASSUMPTION)]) == Health.AT_RISK


def test_outdated_applies_only_to_superseded_side():
    a = alert(kind=AlertKind.SUPERSESSION, decision_ids=["1", "2"], superseded_id="1", superseding_id="2")
    assert derive_health(decision("1"), [a]) == Health.OUTDATED
    assert derive_health(decision("2"), [a]) == Health.HEALTHY


def test_unrelated_alerts_leave_decision_healthy():
    assert derive_health(decision("1"), [alert(kind=AlertKind.CONFLICT, decision_ids=["7"])]) == Health.HEALTHY


# --- topics -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "equal"),
    [("API 規範", "api規範", True), ("ＡＰＩ　規範", "API 規範", True), ("v2 上線時程", "v2 上線 時程", True), ("DB 選擇", "資料庫選型", False)],
)
def test_normalize_topic_ignores_width_case_and_whitespace(a, b, equal):
    assert (normalize_topic(a) == normalize_topic(b)) is equal
    assert same_topic(a, b) is equal  # same_topic keeps its meaning after the move to domain


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (AlertKind.CONFLICT, TopicHealth.ALERT),
        (AlertKind.DEVIATION, TopicHealth.ALERT),
        (AlertKind.ASSUMPTION, TopicHealth.DOUBT),
        (AlertKind.SUPERSESSION, TopicHealth.DOUBT),
    ],
)
def test_topic_health_follows_open_alert_kinds(kind, expected):
    assert derive_topic_health([decision("1"), decision("2")], [alert(kind=kind, decision_ids=["2"])]) is expected


def test_topic_health_red_wins_over_yellow():
    alerts = [alert("1", kind=AlertKind.ASSUMPTION), alert("2", kind=AlertKind.CONFLICT)]
    assert derive_topic_health([decision("1")], alerts) is TopicHealth.ALERT


def test_topic_health_yellow_when_a_decision_is_under_review():
    assert derive_topic_health([decision("1", status=REVIEW)], []) is TopicHealth.DOUBT


def test_topic_health_ignores_alerts_of_other_decisions_and_defaults_to_stable():
    assert derive_topic_health([decision("1")], [alert(kind=AlertKind.CONFLICT, decision_ids=["9"])]) is TopicHealth.STABLE
    assert derive_topic_health([], []) is TopicHealth.STABLE
