import pytest
from pydantic import ValidationError

from decision_ledger.domain.models import Alert, AlertKind, Decision, Evidence, utcnow
from tests.factories import alert, decision, ev, project, topic


def test_decision_requires_at_least_one_evidence():
    with pytest.raises(ValidationError):
        Decision(id="1", topic_id="T1", topic="t", statement="s", rationale="r", decided_by="a", evidence=[])


def test_decision_requires_a_topic_id():
    with pytest.raises(ValidationError, match="topic_id"):
        Decision(id="1", topic="t", statement="s", rationale="r", decided_by="a", evidence=[ev()])


def test_alert_requires_at_least_one_evidence():
    with pytest.raises(ValidationError):
        Alert(id="1", kind=AlertKind.CONFLICT, decision_ids=["1"], summary="s", evidence=[])


def test_alert_requires_a_decision():
    with pytest.raises(ValidationError):
        Alert(id="1", kind=AlertKind.CONFLICT, decision_ids=[], summary="s", evidence=[ev()])


def test_evidence_requires_quote():
    with pytest.raises(ValidationError):
        Evidence(source_id="S1", locator="00:00:01", quote="")


def test_valid_decision_and_alert_build():
    assert decision().evidence[0].quote == "原文"
    assert alert().status == "open"


def test_utcnow_is_timezone_aware():
    assert utcnow().tzinfo is not None


def test_topic_and_project_defaults():
    assert topic().merged_into is None
    assert project(repo="o/r").repo == "o/r"
