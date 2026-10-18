import pytest

from decision_ledger.domain.errors import InvalidTopicOperation, LLMTransportError, NotFound
from decision_ledger.domain.models import AlertStatus, DecisionStatus
from decision_ledger.services.drafts import DecisionDraft, VerifiedQuote
from decision_ledger.services.topics import TopicAssigner, TopicChoice, TopicService
from tests.factories import alert, decision, topic
from tests.fakes import FakeLLM, FakeRepository


def draft(index=0, topic_text="v2 上線時程"):
    q = VerifiedQuote(segment_index=0, locator="00:01:00", speaker="Alice", quote="原文")
    return DecisionDraft(index=index, topic=topic_text, statement="s", rationale="r", decided_by="Alice", evidence=[q])


# --- TopicAssigner ---------------------------------------------------------------


def test_exact_normalized_name_short_circuits_without_llm():
    llm = FakeLLM()
    out = TopicAssigner(llm).assign([draft(topic_text="V2 上線 時程")], [topic("T1", "v2 上線時程")], content_hash="h")
    assert out == [TopicChoice("T1", "v2 上線時程")]
    assert llm.calls == []


def test_no_existing_topics_creates_new_without_llm():
    llm = FakeLLM()
    assert TopicAssigner(llm).assign([draft()], [], content_hash="h") == [TopicChoice(None, "v2 上線時程")]
    assert llm.calls == []


def test_llm_assigns_differently_worded_subject_to_existing_topic():
    llm = FakeLLM({"topic:h": {"assignments": [{"index": 0, "topic_id": "T1"}]}})
    out = TopicAssigner(llm).assign([draft(topic_text="資料庫選型")], [topic("T1", "DB 選擇")], content_hash="h")
    assert out == [TopicChoice("T1", "DB 選擇")]
    assert llm.keys_called() == ["topic:h"]
    assert "資料庫選型" in llm.calls[0][2] and "T1: DB 選擇" in llm.calls[0][2]


def test_llm_null_or_missing_means_new_topic():
    llm = FakeLLM({"topic:h": {"assignments": [{"index": 0, "topic_id": None}]}})
    out = TopicAssigner(llm).assign([draft(0, "部署方式"), draft(1, "監控")], [topic("T1", "DB 選擇")], content_hash="h")
    assert out == [TopicChoice(None, "部署方式"), TopicChoice(None, "監控")]


@pytest.mark.parametrize("target", ["T9", "T2"])
def test_llm_pointing_at_unknown_or_merged_topic_creates_new(target):
    topics = [topic("T1", "DB 選擇"), topic("T2", "舊名", merged_into="T1")]
    llm = FakeLLM({"topic:h": {"assignments": [{"index": 0, "topic_id": target}]}})
    assert TopicAssigner(llm).assign([draft(topic_text="資料庫選型")], topics, content_hash="h") == [TopicChoice(None, "資料庫選型")]


def test_two_failed_attempts_fall_back_to_new_topics():
    llm = FakeLLM({"topic:h": [LLMTransportError("boom"), LLMTransportError("boom")]})
    out = TopicAssigner(llm).assign([draft(topic_text="資料庫選型")], [topic("T1", "DB 選擇")], content_hash="h")
    assert out == [TopicChoice(None, "資料庫選型")]
    assert len(llm.calls) == 2


def test_mixed_batch_keeps_draft_order_and_only_asks_about_unmatched():
    llm = FakeLLM({"topic:h": {"assignments": [{"index": 1, "topic_id": "T1"}]}})
    out = TopicAssigner(llm).assign([draft(0, "API 規範"), draft(1, "資料庫選型")], [topic("T1", "DB 選擇"), topic("T3", "API 規範")], content_hash="h")
    assert out == [TopicChoice("T3", "API 規範"), TopicChoice("T1", "DB 選擇")]
    assert "[0]" not in llm.calls[0][2]


# --- TopicService ----------------------------------------------------------------


def topic_repo():
    repo = FakeRepository()
    repo.topics = {"T1": topic("T1", "DB 選擇"), "T2": topic("T2", "資料庫選型"), "T3": topic("T3", "API 規範")}
    repo.decisions = {
        "1": decision("1", topic_id="T1", statement="用 PostgreSQL"),
        "2": decision("2", topic_id="T2", statement="改用 MySQL", status=DecisionStatus.UNDER_REVIEW),
        "3": decision("3", topic_id="T3"),
    }
    repo.alerts = {"1": alert("1", decision_ids=["1", "2"])}
    return repo


def snapshot(repo):
    return (dict(repo.topics), dict(repo.decisions), dict(repo.alerts), list(repo.events))


def test_merge_moves_every_decision_and_hides_the_source_topic():
    repo = topic_repo()
    statuses = {i: d.status for i, d in repo.decisions.items()}
    target = TopicService(repo).merge("T2", "T1")
    assert target.id == "T1"
    assert {i: d.topic_id for i, d in repo.decisions.items()} == {"1": "T1", "2": "T1", "3": "T3"}
    assert [t.id for t in repo.list_topics()] == ["T1", "T3"]
    # Membership only: statuses, alerts and the audit log are untouched.
    assert {i: d.status for i, d in repo.decisions.items()} == statuses
    assert list(repo.alerts) == ["1"] and repo.alerts["1"].status is AlertStatus.OPEN and repo.events == []


def test_merge_repoints_topics_already_merged_into_the_source():
    repo = topic_repo()
    svc = TopicService(repo)
    svc.merge("T2", "T1")
    svc.merge("T1", "T3")
    assert repo.topics["T2"].merged_into == "T3" and repo.resolve_topic("T2").id == "T3"
    assert {d.topic_id for d in repo.decisions.values()} == {"T3"}


def test_rename_changes_only_the_name():
    repo = topic_repo()
    decisions = dict(repo.decisions)
    renamed = TopicService(repo).rename("T1", "  資料庫  ")
    assert renamed.name == "資料庫" and repo.topics["T1"].name == "資料庫"
    assert repo.decisions == decisions and repo.events == []


def test_rename_to_its_own_name_with_other_spacing_is_allowed():
    repo = topic_repo()
    assert TopicService(repo).rename("T1", "db選擇").name == "db選擇"


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        (lambda svc: svc.merge("T1", "T1"), "itself"),
        (lambda svc: svc.merge("T4", "T1"), "merged"),
        (lambda svc: svc.merge("T1", "T4"), "merged"),
        (lambda svc: svc.rename("T1", "   "), "blank"),
        (lambda svc: svc.rename("T1", "ＡＰＩ 規範"), "merge instead"),
        (lambda svc: svc.rename("T4", "新名稱"), "merged"),
    ],
)
def test_invalid_merge_or_rename_is_rejected_and_changes_nothing(operation, match):
    repo = topic_repo()
    repo.topics["T4"] = topic("T4", "舊名", merged_into="T2")
    before = snapshot(repo)
    with pytest.raises(InvalidTopicOperation, match=match):
        operation(TopicService(repo))
    assert snapshot(repo) == before


def test_merge_into_another_projects_topic_is_rejected():
    from tests.fakes import FakeLedgerStore

    store = FakeLedgerStore()
    mine, theirs = store.for_project("P1"), store.for_project("P2")
    with mine.unit_of_work() as w:
        w.save_topic(topic("1", "DB 選擇"))
    with theirs.unit_of_work() as w:
        w.save_topic(topic("2", "DB 選擇"))
    with pytest.raises(InvalidTopicOperation, match="does not exist in this project"):
        TopicService(mine).merge("1", "2")
    assert mine.get_topic("1").merged_into is None


def test_unknown_topic_is_not_found():
    with pytest.raises(NotFound):
        TopicService(topic_repo()).rename("T404", "x")
    with pytest.raises(NotFound):
        TopicService(topic_repo()).merge("T404", "T1")


def test_merged_topics_name_goes_to_the_topic_that_absorbed_it():
    """Review R1-2: importing with a merged topic's old name must not re-create it."""
    llm = FakeLLM()
    topics = [topic("T1", "DB 選擇", merged_into="T2"), topic("T2", "資料庫選型")]
    out = TopicAssigner(llm).assign([draft(topic_text="db 選擇")], topics, content_hash="h")
    assert out == [TopicChoice("T2", "資料庫選型")]
    assert llm.calls == []


def test_live_topic_name_wins_over_a_merged_one():
    topics = [topic("T1", "API 規範", merged_into="T2"), topic("T2", "介面"), topic("T3", "API 規範")]
    assert TopicAssigner(FakeLLM()).assign([draft(topic_text="API 規範")], topics, content_hash="h") == [TopicChoice("T3", "API 規範")]


# --- move / split ------------------------------------------------------------------


def three_decision_repo():
    repo = topic_repo()
    repo.decisions["4"] = decision("4", topic_id="T1", statement="連線池上限 20", status=DecisionStatus.SUPERSEDED)
    repo.decisions["5"] = decision("5", topic_id="T1", statement="每日備份")
    repo.topics["T4"] = topic("T4", "舊名", merged_into="T2")
    return repo


def test_move_one_decision_changes_only_its_topic():
    repo = three_decision_repo()
    statuses = {i: d.status for i, d in repo.decisions.items()}
    target = TopicService(repo).move("T1", ["5"], "T2")
    assert target.id == "T2"
    assert {i: d.topic_id for i, d in repo.decisions.items()} == {"1": "T1", "2": "T2", "3": "T3", "4": "T1", "5": "T2"}
    assert {i: d.status for i, d in repo.decisions.items()} == statuses
    assert list(repo.alerts) == ["1"] and repo.events == []


def test_move_several_including_a_superseded_decision_and_dedupes_ids():
    repo = three_decision_repo()
    TopicService(repo).move("T1", ["4", "5", "4"], "T3")
    assert repo.decisions["4"].topic_id == "T3" and repo.decisions["4"].status is DecisionStatus.SUPERSEDED
    assert repo.decisions["5"].topic_id == "T3" and repo.decisions["1"].topic_id == "T1"


def test_move_into_an_emptied_topic_is_allowed_and_it_reappears():
    repo = three_decision_repo()
    svc = TopicService(repo)
    svc.move("T2", ["2"], "T1")  # T2 is now empty
    assert svc.move("T1", ["5"], "T2").id == "T2"
    assert repo.decisions["5"].topic_id == "T2"


def test_split_moves_selected_decisions_into_a_new_topic():
    repo = three_decision_repo()
    statuses = {i: d.status for i, d in repo.decisions.items()}
    created = TopicService(repo).split("T1", ["5", "5"], "  備份策略 ")
    assert created.name == "備份策略" and created.merged_into is None
    assert repo.decisions["5"].topic_id == created.id
    assert {repo.decisions[i].topic_id for i in ("1", "4")} == {"T1"}
    assert {i: d.status for i, d in repo.decisions.items()} == statuses and repo.events == []


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        (lambda svc: svc.move("T1", ["1"], "T1"), "already in that topic"),
        (lambda svc: svc.move("T1", ["1"], "T4"), "merged"),
        (lambda svc: svc.move("T1", ["1"], "T404"), "does not exist in this project"),
        (lambda svc: svc.move("T4", ["2"], "T1"), "merged"),
        (lambda svc: svc.move("T1", [], "T2"), "at least one"),
        (lambda svc: svc.split("T1", ["1"], "   "), "blank"),
        (lambda svc: svc.split("T1", ["1"], "ＡＰＩ 規範"), "move the decisions instead"),
        (lambda svc: svc.split("T1", ["1", "4", "5", "1"], "全部"), "rename instead"),
        (lambda svc: svc.split("T1", [], "新主題"), "at least one"),
    ],
)
def test_invalid_move_or_split_is_rejected_and_changes_nothing(operation, match):
    repo = three_decision_repo()
    before = snapshot(repo)
    with pytest.raises(InvalidTopicOperation, match=match):
        operation(TopicService(repo))
    assert snapshot(repo) == before


@pytest.mark.parametrize(
    "operation",
    [
        lambda svc: svc.move("T404", ["1"], "T2"),
        lambda svc: svc.move("T1", ["999"], "T2"),
        lambda svc: svc.move("T1", ["5", "2"], "T3"),  # #2 lives in T2, not in the source topic
        lambda svc: svc.split("T404", ["1"], "新主題"),
        lambda svc: svc.split("T1", ["3"], "新主題"),
    ],
)
def test_unknown_source_or_foreign_decision_is_not_found_and_changes_nothing(operation):
    repo = three_decision_repo()
    before = snapshot(repo)
    with pytest.raises(NotFound):
        operation(TopicService(repo))
    assert snapshot(repo) == before


def test_same_ids_in_two_projects_only_act_on_this_project():
    from tests.fakes import FakeLedgerStore

    store = FakeLedgerStore()
    mine, theirs = store.for_project("P1"), store.for_project("P2")
    for repo in (mine, theirs):  # ids are numbered per project, so both hold topics 1, 2 and decision 1
        with repo.unit_of_work() as w:
            w.save_topic(topic("1", "DB 選擇"))
            w.save_topic(topic("2", "資料庫選型"))
            w.save_decision(decision("1", topic_id="1"))
    TopicService(mine).move("1", ["1"], "2")
    assert mine.get_decision("1").topic_id == "2"
    assert theirs.get_decision("1").topic_id == "1"


def test_project_ids_are_numbered_per_project():
    from tests.fakes import FakeLedgerStore

    store = FakeLedgerStore()
    a, b = store.for_project("P1"), store.for_project("P2")
    with a.unit_of_work() as w:
        assert [w.allocate_id("decision"), w.allocate_id("decision")] == ["1", "2"]
    with b.unit_of_work() as w:
        assert w.allocate_id("decision") == "1"


def test_split_reports_foreign_decisions_as_not_found_before_the_all_selected_check():
    """Review R1-2: an id outside the source topic is a 404, even when the rest selects everything."""
    repo = three_decision_repo()
    with pytest.raises(NotFound):
        TopicService(repo).split("T1", ["1", "4", "5", "999"], "新主題")
    TopicService(repo).move("T2", ["2"], "T1")  # T2 is now empty
    with pytest.raises(NotFound):
        TopicService(repo).split("T2", ["2"], "新主題")


def test_an_emptied_topic_does_not_block_its_name():
    """Review R1-1: the hidden, emptied topic is reused by split and folded away by rename."""
    repo = three_decision_repo()
    svc = TopicService(repo)
    svc.move("T2", ["2"], "T1")  # 資料庫選型 (T2) is now empty and hidden
    reused = svc.split("T1", ["5"], "資料庫選型")
    assert reused.id == "T2" and repo.decisions["5"].topic_id == "T2"
    svc.move("T2", ["5"], "T1")  # empty again
    svc.rename("T3", "資料庫選型")
    assert repo.topics["T3"].name == "資料庫選型" and repo.topics["T2"].merged_into == "T3"
    assert repo.find_topic_by_name("資料庫選型").id == "T3"
