import pytest

from decision_ledger.domain.errors import NotFound
from decision_ledger.domain.models import (
    AlertKind,
    AlertStatus,
    Assumption,
    DecisionEvent,
    DecisionStatus,
    Health,
    PrCheck,
    Resolution,
    ResolutionAction,
    Segment,
    Source,
    SourceKind,
    TopicHealth,
)
from decision_ledger.services.qa import LedgerQuery, QAService, terms
from tests.factories import alert, decision, ev, topic
from tests.fakes import FakeLLM, FakeRepository


def ledger():
    repo = FakeRepository()
    repo.topics["T1"] = topic("T1", "v2 上線時程")
    repo.topics["T2"] = topic("T2", "前端技術選型")
    repo.decisions["17"] = decision("17", statement="v2 在 10/15 上線", status=DecisionStatus.SUPERSEDED)
    repo.decisions["42"] = decision("42", statement="v2 上線延到 11/1")
    repo.decisions["31"] = decision("31", topic="前端", topic_id="T2", statement="前端改用 Next.js")
    return repo


def test_terms_extracts_ascii_words_and_cjk_bigrams_without_question_words():
    t = terms("為什麼 v2 延期？")
    assert "v2" in t and "延期" in t
    assert "為什" not in t and "什麼" not in t


def test_answer_cites_matching_decision_with_evidence():
    repo = ledger()
    llm = FakeLLM({"AnswerDraft": {"answer": "因為 QA 人力不足，延到 11/1。", "cited_decision_ids": ["42"]}})
    answer = QAService(repo, llm).answer("為什麼 v2 延期？")
    assert answer.found and answer.text.startswith("因為")
    assert [c.decision_id for c in answer.citations] == ["42"]
    assert answer.citations[0].evidence
    assert "#42" in llm.calls[0][2]
    assert "topic: v2 上線時程" in llm.calls[0][2]  # the topic's display name, not the raw extracted wording


def test_unrelated_question_returns_not_found_without_llm():
    llm = FakeLLM()
    answer = QAService(ledger(), llm).answer("午餐吃什麼？")
    assert not answer.found and answer.text == "No related decision found" and llm.calls == []


def test_citations_to_unknown_decisions_are_dropped():
    llm = FakeLLM({"AnswerDraft": {"answer": "x", "cited_decision_ids": ["42", "999", "#17"]}})
    answer = QAService(ledger(), llm).answer("v2 延期")
    assert [c.decision_id for c in answer.citations] == ["42", "17"]


def test_answer_without_valid_citation_is_not_found():
    llm = FakeLLM({"AnswerDraft": {"answer": "編造的答案", "cited_decision_ids": []}})
    answer = QAService(ledger(), llm).answer("v2 延期")
    assert not answer.found and answer.text == "No related decision found"


def test_ask_key_is_deterministic():
    llm = FakeLLM()
    svc = QAService(ledger(), llm)
    svc.answer("v2 延期")
    svc.answer(" v2 延期 ")
    keys = llm.keys_called()
    assert keys[0] == keys[1] and keys[0].startswith("ask:")


# --- LedgerQuery ----------------------------------------------------------


def query_repo():
    repo = ledger()
    repo.alerts["1"] = alert("1", AlertKind.CONFLICT, ["42", "31"])
    repo.alerts["2"] = alert("2", AlertKind.ASSUMPTION, ["31"], status=AlertStatus.RESOLVED)
    repo.sources["S1"] = Source(id="S1", kind=SourceKind.TRANSCRIPT, title="週會", content_hash="h",
                                segments=[Segment(index=0, locator="00:01:00", speaker="A", text="t")])
    repo.pr_checks["k"] = PrCheck(key="k", diff="d", comment="c", violated=False)
    return repo


def test_overview_counts_health_and_splits_alerts():
    overview = LedgerQuery(query_repo(), backend="fake").overview()
    assert overview.backend == "fake"
    assert [(src.id, src.title) for src in overview.sources] == [("S1", "週會")]
    assert overview.health_counts[Health.CONFLICT] == 2
    assert overview.health_counts[Health.SUPERSEDED] == 1
    assert [a.id for a in overview.open_alerts] == ["1"]
    assert [a.id for a in overview.resolved_alerts] == ["2"]
    assert overview.decisions[0].health is Health.CONFLICT
    assert overview.decisions[-1].health is Health.SUPERSEDED


def test_list_returns_decisions_with_health():
    views = LedgerQuery(query_repo(), backend="fake").list()
    assert {v.decision.id: v.health for v in views}["17"] is Health.SUPERSEDED


def test_overview_tags_each_alert_with_its_topic():
    overview = LedgerQuery(query_repo(), backend="fake", project="v2 專案").overview()
    assert overview.project == "v2 專案"
    assert {k: t.name for k, t in overview.alert_topics.items()} == {"1": "v2 上線時程", "2": "前端技術選型"}


def test_topic_cards_are_sorted_red_yellow_green_with_counts():
    repo = query_repo()
    repo.topics["T3"] = topic("T3", "API 規範")
    repo.decisions["50"] = decision("50", topic_id="T3", statement="snake_case", assumptions=["前端會跟著改"])
    repo.alerts["3"] = alert("3", AlertKind.ASSUMPTION, ["50"])
    repo.topics["T4"] = topic("T4", "舊名", merged_into="T1")
    cards = LedgerQuery(repo, backend="fake").topics()
    assert [(c.topic.id, c.health) for c in cards] == [
        ("T1", TopicHealth.ALERT), ("T2", TopicHealth.ALERT), ("T3", TopicHealth.DOUBT)
    ]  # merged T4 is not listed
    v2 = cards[0]
    assert [v.decision.id for v in v2.current] == ["42"]
    assert [p.decision_id for p in v2.timeline] == ["17", "42"] and v2.decision_count == 2
    assert (v2.assumption_count, v2.source_count, v2.pending_count) == (0, 1, 1)
    assert cards[2].assumption_count == 1


def test_mini_timeline_keeps_the_latest_five():
    repo = FakeRepository()
    repo.topics["T1"] = topic()
    for i in range(1, 8):
        repo.decisions[str(i)] = decision(str(i))
    card = LedgerQuery(repo, backend="fake").topics()[0]
    assert [p.decision_id for p in card.timeline] == ["3", "4", "5", "6", "7"] and card.decision_count == 7


def test_topic_detail_orders_nodes_and_derives_change_types():
    repo = ledger()
    repo.decisions["17"] = decision("17", statement="v2 在 10/15 上線")
    repo.decisions["43"] = decision("43", statement="照原計畫 10/15")
    repo.alerts["1"] = alert("1", AlertKind.SUPERSESSION, ["17", "42"], superseded_id="17", superseding_id="42")
    repo.alerts["2"] = alert("2", AlertKind.CONFLICT, ["42", "43"])
    repo.alerts["3"] = alert("3", AlertKind.CONFLICT, ["17", "31"], status=AlertStatus.OBSOLETE)
    repo.events.append(DecisionEvent(id=1, decision_id="42", kind="merged"))
    detail = LedgerQuery(repo, backend="fake").topic_detail("T1")
    assert detail.health is TopicHealth.ALERT
    nodes = {n.decision.id: n for n in detail.nodes}
    assert list(nodes) == ["17", "42", "43"]
    assert [c.kind for c in nodes["17"].changes] == ["added"]
    assert [(c.kind, c.ref, c.count) for c in nodes["42"].changes] == [("updates", "17", 0), ("merged_sources", None, 1)]
    assert [(c.kind, c.ref) for c in nodes["43"].changes] == [("conflicts", "42")]
    # Each alert sits next to one node: the replaced decision, or the newer side of a conflict; obsolete ones are hidden.
    assert [a.id for a in nodes["17"].alerts] == ["1"]
    assert [a.id for a in nodes["43"].alerts] == ["2"] and nodes["42"].alerts == []
    assert nodes["42"].health is Health.CONFLICT


def test_topic_detail_assumption_states():
    repo = FakeRepository()
    repo.topics["T1"] = topic()
    repo.sources["S2"] = Source(id="S2", kind=SourceKind.TRANSCRIPT, title="週會", occurred_on="10/10", content_hash="h2")
    premise = lambda text: Assumption(text=text, evidence=[ev("當初的話")])  # noqa: E731
    repo.decisions["1"] = decision("1").model_copy(
        update={"assumptions": [premise("QA 只有 2 人"), premise("採購季在 10 月"), premise("預算足夠"), premise("沒有人提到")]}
    )
    new_info = ev("QA 新人報到", source_id="S2")
    repo.alerts["1"] = alert("1", AlertKind.ASSUMPTION, ["1"], assumption_text="QA 只有 2 人", evidence=[ev("當初的話"), new_info])
    repo.alerts["2"] = alert("2", AlertKind.ASSUMPTION, ["1"], assumption_text="採購季在 10 月", evidence=[ev("當初的話"), new_info],
                             status=AlertStatus.RESOLVED, resolution=Resolution(action=ResolutionAction.RECONSIDER))
    repo.alerts["3"] = alert("3", AlertKind.ASSUMPTION, ["1"], assumption_text="預算足夠", evidence=[ev("當初的話")],
                             status=AlertStatus.RESOLVED, resolution=Resolution(action=ResolutionAction.ASSUMPTION_HOLDS))
    views = {a.text: a for a in LedgerQuery(repo, backend="fake").topic_detail("T1").assumptions}
    assert views["QA 只有 2 人"].state == "doubted" and views["QA 只有 2 人"].evidence == [new_info]
    assert views["採購季在 10 月"].state == "overturned" and views["採購季在 10 月"].on == "10/10"
    assert views["預算足夠"].state == "confirmed" and views["沒有人提到"].state == "holds"


def test_merged_topic_detail_points_at_the_target_and_unknown_is_not_found():
    repo = ledger()
    repo.topics["T9"] = topic("T9", "舊名", merged_into="T1")
    detail = LedgerQuery(repo, backend="fake").topic_detail("T9")
    assert detail.merged_into.id == "T1" and detail.nodes == []
    with pytest.raises(NotFound):
        LedgerQuery(repo, backend="fake").topic_detail("T404")


def test_alerts_filters_by_status():
    q = LedgerQuery(query_repo(), backend="fake")
    assert [a.id for a in q.alerts(AlertStatus.OPEN)] == ["1"]
    assert len(q.alerts()) == 2


def test_pr_checks_and_source():
    q = LedgerQuery(query_repo(), backend="fake")
    assert [c.key for c in q.pr_checks()] == ["k"]
    assert q.source("S1").segments[0].locator == "00:01:00"
    with pytest.raises(NotFound):
        q.source("nope")


def test_timelines_follow_decision_dates_not_import_order():
    """Review R1-4: an older meeting imported later still appears first; undated decisions go last."""
    repo = FakeRepository()
    repo.topics["T1"] = topic()
    dated = {"1": "10/10", "2": "9/16–9/20", "3": None, "4": "9/1"}
    for i, on in dated.items():
        repo.decisions[i] = decision(i).model_copy(update={"decided_on": on})
    q = LedgerQuery(repo, backend="fake")
    assert [n.decision.id for n in q.topic_detail("T1").nodes] == ["4", "2", "1", "3"]
    assert [p.decision_id for p in q.topics()[0].timeline] == ["4", "2", "1", "3"]


def test_full_dates_order_across_years():
    """Review R2-1: 2025-12-20 comes before 2026-01-05."""
    repo = FakeRepository()
    repo.topics["T1"] = topic()
    for i, on in {"1": "2026-01-05", "2": "2025-12-20", "3": "2026/03/01"}.items():
        repo.decisions[i] = decision(i).model_copy(update={"decided_on": on})
    assert [n.decision.id for n in LedgerQuery(repo, backend="fake").topic_detail("T1").nodes] == ["2", "1", "3"]


def test_emptied_topics_are_hidden_from_cards_but_still_open():
    repo = ledger()
    repo.topics["T3"] = topic("T3", "空主題")
    q = LedgerQuery(repo, backend="fake")
    assert "T3" not in {c.topic.id for c in q.topics()}
    detail = q.topic_detail("T3")
    assert detail.topic.name == "空主題" and detail.nodes == [] and detail.merged_into is None


def test_english_question_words_are_not_search_terms():
    """Review R1-04: substring search would match "is"/"the" in nearly every decision."""
    assert terms("Why did the beta launch move to November?") == ["beta", "launch", "move", "november"]


def test_unrelated_english_question_returns_not_found_without_llm():
    llm = FakeLLM({})
    answer = QAService(ledger(), llm).answer("What is for lunch today?")
    assert not answer.found and answer.text == "No related decision found" and llm.calls == []
