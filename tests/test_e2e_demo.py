"""End-to-end with the real SQLite adapter and Fake backend, no credentials (SC-001 … SC-005).

The Fake backend replays responses recorded from Gemini (demo/fixtures/llm/RECORDING.md),
so the expected counts below are the recorded outcome, not hand-written answers.
"""

import json
from pathlib import Path

import pytest

from decision_ledger.bootstrap import DEMO_DIR, Settings, build_provider, run_demo
from decision_ledger.domain.models import (
    AlertKind,
    DecisionStatus,
    PullRequestDiff,
    Resolution,
    ResolutionAction,
    SourceKind,
    TopicHealth,
)

WORDING = Path(__file__).resolve().parent / "fixtures" / "topic_wording"


@pytest.fixture
def provider(tmp_path):
    provider, project = build_provider(Settings.from_env({"DL_DB_PATH": str(tmp_path / "demo.db")}))
    provider.default_id = project.id
    return provider


@pytest.fixture
def container(provider):
    return provider.for_project(provider.default_id)


# Recorded outcome (RECORDING.md, re-recorded 2026-10-17): all three demo moments are present.
EXPECTED = {"decisions": 5, "supersession": 1, "conflict": 1, "assumption": 3, "deviation": 1}


def test_demo_produces_expected_alerts_without_credentials(container):
    summary = run_demo(container)
    assert summary == EXPECTED


def test_premise_moment_points_at_the_november_17_decision(container):
    run_demo(container)
    statements = {v.decision.id: v.decision.statement for v in container.query.list()}
    alerts = container.query.overview().open_alerts
    premise_targets = {statements[a.decision_ids[0]] for a in alerts if a.kind is AlertKind.ASSUMPTION}
    assert any("November 17" in s for s in premise_targets)  # QA went from two testers to four


def test_conflict_moment_guest_checkout_vs_account_required(container):
    run_demo(container)
    statements = {v.decision.id: v.decision.statement for v in container.query.list()}
    conflicts = [a for a in container.query.overview().open_alerts if a.kind is AlertKind.CONFLICT]
    assert any(
        all("guest" in statements[i].lower() or "account" in statements[i].lower() for i in a.decision_ids) for a in conflicts
    )


def test_demo_groups_decisions_into_topic_cards(container):
    run_demo(container)
    cards = container.query.topics()
    assert len(cards) >= 3 and all(c.decision_count for c in cards)
    assert [c.health for c in cards] == sorted((c.health for c in cards), key=[TopicHealth.ALERT, TopicHealth.DOUBT, TopicHealth.STABLE].index)
    assert any(c.health is not TopicHealth.STABLE for c in cards)
    assert all(container.query.overview().alert_topics.get(a.id) for a in container.query.overview().open_alerts)


def test_every_decision_and_alert_has_evidence(container):
    run_demo(container)
    overview = container.query.overview()
    assert all(v.decision.evidence for v in overview.decisions)
    assert all(a.evidence for a in overview.open_alerts + overview.resolved_alerts)


def test_demo_is_idempotent_on_rerun(container):
    run_demo(container)
    assert run_demo(container) == EXPECTED


def test_confirmed_supersession_marks_old_decision_on_the_topic_timeline(container):
    run_demo(container)
    alert = next(a for a in container.query.alerts() if a.kind is AlertKind.SUPERSESSION)
    container.alerts.resolve(alert.id, Resolution(action=ResolutionAction.CONFIRM_SUPERSESSION))
    topic_id = container.query.repo.get_decision(alert.superseding_id).topic_id
    nodes = {n.decision.id: n for n in container.query.topic_detail(topic_id).nodes}
    old = nodes[alert.superseded_id].decision
    assert old.status is DecisionStatus.SUPERSEDED and old.superseded_by == alert.superseding_id
    assert [c.kind for c in nodes[alert.superseding_id].changes][0] == "updates"


def test_demo_question_cites_decision_with_evidence(container):
    run_demo(container)
    answer = container.qa.answer("Why did the beta launch move?")
    assert answer.found and answer.citations[0].evidence


def test_pr_88_passes(container):
    run_demo(container)
    checks = {c.pr_number: c for c in container.query.pr_checks()}
    assert set(checks) == {87, 88} and not checks[88].violated
    assert "no violation found" in checks[88].comment


def test_pr_87_violates_the_payments_boundary(container):
    run_demo(container)
    checks = {c.pr_number: c for c in container.query.pr_checks()}
    assert checks[87].violated
    # Review L2-01: the deviation must point at the payments boundary decision, not just any decision.
    statements = {v.decision.id: v.decision.statement.lower() for v in container.query.list()}
    deviations = [a for a in container.query.overview().open_alerts if a.kind is AlertKind.DEVIATION]
    assert deviations and all("src/payments" in statements[i] for a in deviations for i in a.decision_ids)


def test_two_projects_with_the_same_topics_never_meet(provider, container):
    """SC-005: reconciliation, assumption watch, PR checks and Q&A all stay inside one project."""
    before = run_demo(container)
    other = provider.for_project(provider.projects.create("Mobile app").id)
    steps = {s.get("file"): s for s in json.loads((DEMO_DIR / "manifest.json").read_text(encoding="utf-8"))["steps"]}

    def ingest(name):
        step = steps[name]
        return other.ingestion.ingest(SourceKind(step["kind"]), step["title"], (DEMO_DIR / name).read_text(encoding="utf-8"), step["occurred_on"])

    first = ingest("01-kickoff-0908.txt")  # same source as project A: not a duplicate here
    assert not first.duplicate and first.created
    ingest("05-weekly-1013.txt")  # contradicts a premise of A's decision, but B's watch sees only B
    pr = next(s for s in steps.values() if s["type"] == "prs")["pull_requests"][0]
    check = other.deviation.check(PullRequestDiff(**{k: v for k, v in pr.items() if k != "file"}, diff=(DEMO_DIR / pr["file"]).read_text(encoding="utf-8")))

    mine, theirs = container.query.topics(), other.query.topics()
    assert {c.topic.name for c in theirs} <= {c.topic.name for c in mine}
    # Ids are numbered per project, so the same id appears in both — but each row belongs to its own project.
    assert all(c.topic.project_id == other.query.repo.project_id for c in theirs)
    assert all(c.topic.project_id == container.query.repo.project_id for c in mine)
    assert other.query.overview().open_alerts == []
    assert not check.check.violated and not check.reused
    b_ids = {v.decision.id for v in other.query.list()}
    assert all(c.decision_id in b_ids for c in other.qa.answer("Why can't payments code use the database client directly?").citations)
    assert run_demo(container) == before  # project A is unchanged


def test_differently_worded_decision_joins_the_existing_topic(tmp_path):
    """SC-002: "DB 選擇" and "資料庫選型" end up on one timeline, and the newer one supersedes the older."""
    settings = Settings.from_env({"DL_DB_PATH": str(tmp_path / "w.db"), "DL_FIXTURES_DIR": str(WORDING)})
    provider, project = build_provider(settings)
    c = provider.for_project(project.id)
    c.ingestion.ingest(SourceKind.TRANSCRIPT, "9/1", (WORDING / "01-db.txt").read_text(encoding="utf-8"), "9/1")
    report = c.ingestion.ingest(SourceKind.TRANSCRIPT, "9/15", (WORDING / "02-db.txt").read_text(encoding="utf-8"), "9/15")
    assert (report.topics_existing, report.topics_created) == (1, 0)
    [card] = c.query.topics()
    assert card.topic.name == "DB 選擇" and card.decision_count == 2
    assert len(report.supersession_alerts) == 1 and card.health is TopicHealth.DOUBT


def test_demo_reproduces_in_a_second_project_after_other_imports(provider, container):
    """SC-002 / SC-003: ids are numbered per project, so the recorded demo answers fit any empty project."""
    assert run_demo(container) == EXPECTED  # the default project already used decision #1–#5
    second = provider.for_project(provider.projects.create("Mobile app").id)
    assert run_demo(second) == EXPECTED
    assert sorted(int(v.decision.id) for v in second.query.list()) == [1, 2, 3, 4, 5]


def test_recorded_answers_cite_decisions_on_the_asked_subject(container):
    """Regression pin for review R1-01: recorded answers must cite the decisions they talk about.

    Answers are keyed by the question text only, so a recording made while decision ids
    were numbered differently replays with citations pointing at the wrong decisions.
    """
    run_demo(container)
    manifest = json.loads((DEMO_DIR / "manifest.json").read_text(encoding="utf-8"))
    subject = {
        "Why did the beta launch move?": "november",
        "How did the beta launch date change over time?": "november",
        "Why can't payments code use the database client directly?": "payments",
        "Is guest checkout allowed in the beta?": "guest",
    }
    statements = {v.decision.id: v.decision.statement.lower() for v in container.query.list()}
    for question in manifest["questions"]:
        answer = container.qa.answer(question)
        assert answer.found and answer.citations, question
        assert all(subject[question] in statements[c.decision_id] for c in answer.citations), (question, answer.text)
    guest = container.qa.answer("Is guest checkout allowed in the beta?")
    cited = {statements[c.decision_id] for c in guest.citations}
    assert any("disabled" in s or "requir" in s for s in cited)  # the answer says no, so it must cite the account-required decision
