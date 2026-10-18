import pytest

from decision_ledger.domain.errors import LLMOutputError, ParseError
from decision_ledger.domain.models import AlertKind, DecisionStatus, SourceKind
from decision_ledger.services.assumption_watch import AssumptionWatchService
from decision_ledger.services.ingestion import IngestionService
from decision_ledger.services.parsers import content_hash
from decision_ledger.services.reconciliation import ReconciliationService
from tests.fakes import FakeLLM, FakeRepository

T1 = """[00:08:02] Alice: 客戶的年度採購季在 10 月下旬，我們要趕在那之前。
[00:08:12] Alice: 那 v2 就定 10/15 上線。
[00:11:30] Dave: API 回傳欄位統一用 snake_case，不要再混用了。"""

T2 = """[00:12:02] Carol: QA 現在只有兩個人，回歸測試一定做不完。
[00:12:34] Alice: 那就延到 11/1 吧，品質比較重要。"""

T3 = """[00:05:12] Carol: 好消息，QA 新人這週報到了，現在有 3 個人。"""


def q(i, quote):
    return {"segment_index": i, "quote": quote}


def extracted(topic, statement, quotes, assumptions=None, rule=None):
    return {"topic": topic, "statement": statement, "rationale": "理由", "decided_by": "Alice",
            "evidence": quotes, "assumptions": assumptions or [], "checkable_rule": rule}


def service(llm, repo=None):
    repo = repo or FakeRepository()
    return IngestionService(repo, llm, ReconciliationService(llm), AssumptionWatchService(llm)), repo


def first_import(llm_extra=None):
    h1 = content_hash(T1)
    responses = {
        f"extract:{h1}": {"decisions": [
            extracted("v2 上線時程", "v2 在 10/15 上線", [q(1, "那 v2 就定 10/15 上線")],
                      assumptions=[{"text": "客戶採購季在 10 月下旬", "evidence": [q(0, "客戶的年度採購季在 10 月下旬")]}]),
            extracted("API 規範", "API 回傳欄位一律使用 snake_case", [q(2, "API 回傳欄位統一用 snake_case")],
                      rule={"description": "JSON 欄位必須為 snake_case", "path_patterns": ["src/api/*"]}),
        ]},
        **(llm_extra or {}),
    }
    llm = FakeLLM(responses)
    svc, repo = service(llm)
    report = svc.ingest(SourceKind.TRANSCRIPT, "9/1 v2 產品週會", T1, occurred_on="9/1")
    return svc, repo, llm, report


def test_import_creates_decisions_with_evidence():
    _, repo, _, report = first_import()
    assert report.segment_count == 3 and len(report.created) == 2 and report.discarded == []
    d = repo.decisions[report.created[0]]
    assert d.statement == "v2 在 10/15 上線"
    assert d.decided_on == "9/1"
    assert d.evidence[0].locator == "00:08:12" and d.evidence[0].source_id == report.source_id
    assert d.evidence[0].quote == "那 v2 就定 10/15 上線"
    assert d.assumptions[0].evidence[0].locator == "00:08:02"
    assert repo.decisions[report.created[1]].checkable_rule.path_patterns == ["src/api/*"]
    assert [e.kind for e in repo.events] == ["created", "created"]


def test_extraction_prompt_lists_existing_topics():
    svc, repo, llm, _ = first_import()
    svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    prompt = [p for m, _, p in llm.calls if m == "ExtractionResult"][-1]
    assert "v2 上線時程" in prompt and "API 規範" in prompt


def test_decision_without_verifiable_evidence_is_discarded():
    h = content_hash(T2)
    llm = FakeLLM({f"extract:{h}": {"decisions": [
        extracted("x", "沒有出處", []),
        extracted("y", "引文不存在", [q(0, "這句話不在逐字稿裡")]),
        extracted("v2 上線時程", "v2 上線延到 11/1", [q(1, "那就延到 11/1 吧")]),
    ]}})
    svc, repo = service(llm)
    report = svc.ingest(SourceKind.TRANSCRIPT, "t", T2)
    assert len(report.created) == 1
    assert [d.summary for d in report.discarded] == ["沒有出處", "引文不存在"]
    assert all(d.kind == "decision" for d in report.discarded)
    assert len(repo.decisions) == 1


def test_transcript_without_decisions_imports_zero():
    llm = FakeLLM()
    svc, repo = service(llm)
    report = svc.ingest(SourceKind.TRANSCRIPT, "t", T3)
    assert report.created == [] and report.source_id is not None
    assert len(repo.sources) == 1


def test_duplicate_content_is_reported_and_not_reextracted():
    svc, repo, llm, report = first_import()
    calls = len(llm.calls)
    again = svc.ingest(SourceKind.TRANSCRIPT, "same", T1 + "\n")
    assert again.duplicate and again.source_id == report.source_id and again.created == []
    assert len(llm.calls) == calls and len(repo.decisions) == 2


def test_concurrent_duplicate_on_write_is_reported_as_duplicate():
    llm = FakeLLM()
    svc, repo = service(llm)
    original_find = repo.find_source_by_hash
    repo.find_source_by_hash = lambda h: None  # race: hash check passed before the other writer committed
    svc.ingest(SourceKind.TRANSCRIPT, "a", T3)
    report = svc.ingest(SourceKind.TRANSCRIPT, "b", T3)
    repo.find_source_by_hash = original_find
    assert report.duplicate and len(repo.sources) == 1


def test_llm_failure_twice_writes_nothing():
    llm = FakeLLM({"ExtractionResult": [{"decisions": "bad"}, {"decisions": "bad"}]})
    svc, repo = service(llm)
    with pytest.raises(LLMOutputError):
        svc.ingest(SourceKind.TRANSCRIPT, "t", T1)
    assert repo.sources == {} and repo.decisions == {} and repo.alerts == {}


def test_failure_during_write_rolls_back_everything():
    svc, repo, llm, _ = first_import()
    before = (dict(repo.sources), dict(repo.decisions), len(repo.events))
    llm.responses[f"extract:{content_hash(T3)}"] = {"decisions": [extracted("新主題", "新決策", [q(0, "QA 新人這週報到了")])]}

    def boom():
        raise RuntimeError("disk full")

    repo.before_commit = boom
    with pytest.raises(RuntimeError):
        svc.ingest(SourceKind.TRANSCRIPT, "t3", T3)
    assert (dict(repo.sources), dict(repo.decisions), len(repo.events)) == before


def test_parse_error_propagates_before_any_llm_call():
    llm = FakeLLM()
    svc, _ = service(llm)
    with pytest.raises(ParseError):
        svc.ingest(SourceKind.SLACK, "bad", "not json")
    assert llm.calls == []


def test_merge_adds_evidence_without_new_decision():
    h2 = content_hash(T2)
    svc, repo, llm, first = first_import({
        f"extract:{h2}": {"decisions": [extracted("v2 上線時程", "v2 在 10/15 上線", [q(1, "那就延到 11/1 吧")])]},
        f"reconcile:{h2}:0": {"relation": "merge", "existing_id": "1"},
    })
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    assert report.merged == ["1"] and report.created == []
    assert len(repo.decisions["1"].evidence) == 2
    assert repo.events[-1].kind == "merged"


def test_supersede_creates_decision_and_alert_without_changing_old_status():
    h2 = content_hash(T2)
    svc, repo, llm, _ = first_import({
        f"extract:{h2}": {"decisions": [extracted("v2 上線時程", "v2 上線延到 11/1", [q(1, "那就延到 11/1 吧")])]},
        f"reconcile:{h2}:0": {"relation": "supersede", "existing_id": "1", "explanation": "延期"},
    })
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    new_id = report.created[0]
    alert = repo.alerts[report.supersession_alerts[0]]
    assert alert.kind is AlertKind.SUPERSESSION
    assert (alert.superseded_id, alert.superseding_id) == ("1", new_id)
    assert {e.source_id for e in alert.evidence} == {"1", report.source_id}
    assert repo.decisions["1"].status is DecisionStatus.ACTIVE


def test_conflict_creates_alert_and_keeps_both_active():
    h2 = content_hash(T2)
    svc, repo, llm, _ = first_import({
        f"extract:{h2}": {"decisions": [extracted("v2 上線時程", "v2 照原計畫 10/15 上線", [q(1, "那就延到 11/1 吧")])]},
        f"reconcile:{h2}:0": {"relation": "conflict", "existing_id": "1"},
    })
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    alert = repo.alerts[report.conflict_alerts[0]]
    assert alert.kind is AlertKind.CONFLICT and alert.decision_ids == ["1", report.created[0]]
    assert all(d.status is DecisionStatus.ACTIVE for d in repo.decisions.values())


def test_assumption_alert_has_original_and_new_evidence():
    h2 = content_hash(T2)
    svc, repo, llm, _ = first_import({
        f"assumption:{h2}": {"findings": [{"decision_id": "1", "assumption_text": "客戶採購季在 10 月下旬",
                                           "evidence": [q(0, "QA 現在只有兩個人")]}]},
    })
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    alert = repo.alerts[report.assumption_alerts[0]]
    assert alert.kind is AlertKind.ASSUMPTION and alert.assumption_text == "客戶採購季在 10 月下旬"
    assert [e.locator for e in alert.evidence] == ["00:08:02", "00:12:02"]


def test_assumption_alert_without_evidence_is_discarded_but_import_succeeds():
    h2 = content_hash(T2)
    svc, repo, llm, _ = first_import({
        f"extract:{h2}": {"decisions": [extracted("新主題", "新決策", [q(1, "那就延到 11/1 吧")])]},
        f"assumption:{h2}": {"findings": [{"decision_id": "1", "assumption_text": "客戶採購季在 10 月下旬",
                                           "evidence": [q(0, "不存在")]}]},
    })
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    assert report.assumption_alerts == [] and len(report.created) == 1
    assert report.discarded[0].kind == "assumption_alert"


# --- Review R1-2: the ledger can change while LLM calls are in flight ------


def _supersede_during_call(repo, decision_id, response):
    def call(prompt, key):
        d = repo.decisions[decision_id]
        repo.decisions[decision_id] = d.model_copy(update={"status": DecisionStatus.SUPERSEDED, "superseded_by": "99"})
        return response
    return call


@pytest.mark.parametrize("relation", ["conflict", "supersede", "merge"])
def test_target_superseded_during_llm_call_creates_plain_new_decision(relation):
    h2 = content_hash(T2)
    svc, repo, llm, _ = first_import()
    llm.responses[f"extract:{h2}"] = {"decisions": [extracted("v2 上線時程", "v2 照原計畫 10/15", [q(1, "那就延到 11/1 吧")])]}
    llm.responses[f"reconcile:{h2}:0"] = _supersede_during_call(repo, "1", {"relation": relation, "existing_id": "1"})
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    assert report.conflict_alerts == [] and report.supersession_alerts == [] and report.merged == []
    assert len(report.created) == 1
    assert len(repo.decisions["1"].evidence) == 1
    assert repo.alerts == {}


def test_assumption_alert_dropped_when_decision_changed_during_llm_call():
    h2 = content_hash(T2)
    finding = {"findings": [{"decision_id": "1", "assumption_text": "客戶採購季在 10 月下旬", "evidence": [q(0, "QA 現在只有兩個人")]}]}
    svc, repo, llm, _ = first_import()
    llm.responses[f"assumption:{h2}"] = _supersede_during_call(repo, "1", finding)
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    assert report.assumption_alerts == [] and repo.alerts == {}
    assert report.discarded[-1].kind == "assumption_alert"


# --- Review R1-3: an assumption alert always carries the original side -----


def test_assumption_alert_falls_back_to_decision_evidence_when_assumption_quote_unverified():
    h1, h2 = content_hash(T1), content_hash(T2)
    llm = FakeLLM({
        f"extract:{h1}": {"decisions": [extracted("v2 上線時程", "v2 在 10/15 上線", [q(1, "那 v2 就定 10/15 上線")],
                                                  assumptions=[{"text": "客戶採購季在 10 月下旬", "evidence": [q(0, "逐字稿裡沒有這句")]}])]},
        f"assumption:{h2}": {"findings": [{"decision_id": "1", "assumption_text": "客戶採購季在 10 月下旬",
                                           "evidence": [q(0, "QA 現在只有兩個人")]}]},
    })
    svc, repo = service(llm)
    svc.ingest(SourceKind.TRANSCRIPT, "t1", T1)
    assert repo.decisions["1"].assumptions[0].evidence == []
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    alert = repo.alerts[report.assumption_alerts[0]]
    sources = {e.source_id for e in alert.evidence}
    assert len(sources) == 2  # original side (decision's own evidence) + new information
    assert alert.evidence[0].locator == "00:08:12"


# --- topic assignment -----------------------------------------------------------


def test_first_import_creates_one_topic_per_subject_and_reports_it():
    _, repo, llm, report = first_import()
    topics = {t.name: t.id for t in repo.list_topics()}
    assert set(topics) == {"v2 上線時程", "API 規範"}
    assert {repo.decisions[i].topic_id for i in report.created} == set(topics.values())
    assert (report.topics_created, report.topics_existing) == (2, 0)
    assert llm.keys_called("TopicAssignment") == []  # no existing topics: nothing to ask


def test_same_new_subject_twice_in_one_batch_creates_one_topic():
    h = content_hash(T2)
    llm = FakeLLM({f"extract:{h}": {"decisions": [
        extracted("QA 人力", "QA 只有兩人", [q(0, "QA 現在只有兩個人")]),
        extracted("ＱＡ 人力", "v2 延到 11/1", [q(1, "那就延到 11/1 吧")]),
    ]}})
    svc, repo = service(llm)
    report = svc.ingest(SourceKind.TRANSCRIPT, "t", T2)
    assert len(repo.list_topics()) == 1
    assert (report.topics_created, report.topics_existing) == (1, 1)


def test_exact_name_reuses_topic_without_asking_the_llm():
    h2 = content_hash(T2)
    svc, repo, llm, _ = first_import({f"extract:{h2}": {"decisions": [
        extracted("v2 上線時程", "v2 上線延到 11/1", [q(1, "那就延到 11/1 吧")])]}})
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    assert repo.decisions[report.created[0]].topic_id == repo.find_topic_by_name("v2 上線時程").id
    assert llm.keys_called("TopicAssignment") == [] and report.topics_existing == 1


def test_differently_worded_subject_joins_the_llm_chosen_topic_and_is_reconciled_there():
    h2 = content_hash(T2)
    svc, repo, llm, first = first_import()
    timeline = repo.find_topic_by_name("v2 上線時程")
    llm.responses.update({
        f"extract:{h2}": {"decisions": [extracted("上線日期", "v2 上線延到 11/1", [q(1, "那就延到 11/1 吧")])]},
        f"topic:{h2}": {"assignments": [{"index": 0, "topic_id": timeline.id}]},
        f"reconcile:{h2}:0": {"relation": "supersede", "existing_id": first.created[0]},
    })
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    assert repo.decisions[report.created[0]].topic_id == timeline.id
    assert len(report.supersession_alerts) == 1 and len(repo.list_topics()) == 2


def test_extraction_prompt_uses_current_topic_names():
    svc, repo, llm, _ = first_import()
    with repo.unit_of_work() as w:
        w.rename_topic(repo.find_topic_by_name("API 規範").id, "API 命名規範")
    svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    prompt = [p for m, _, p in llm.calls if m == "ExtractionResult"][-1]
    assert "API 命名規範" in prompt


def test_topic_merged_during_the_llm_calls_is_followed_to_its_target():
    h2 = content_hash(T2)
    svc, repo, llm, _ = first_import()
    timeline = repo.find_topic_by_name("v2 上線時程")
    api = repo.find_topic_by_name("API 規範")
    llm.responses[f"extract:{h2}"] = {"decisions": [extracted("v2 上線時程", "v2 上線延到 11/1", [q(1, "那就延到 11/1 吧")])]}
    original = svc.topic_assigner.assign

    def assign_then_merge(*args, **kwargs):
        choices = original(*args, **kwargs)
        with repo.unit_of_work() as w:  # someone merges the topic before the import writes
            w.merge_topic(timeline.id, api.id)
        return choices

    svc.topic_assigner.assign = assign_then_merge
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    assert repo.decisions[report.created[0]].topic_id == api.id


def test_import_after_merge_keeps_using_the_merge_target():
    """Review R1-2: a later decision worded like the merged topic joins the target; the merge stays."""
    h2 = content_hash(T2)
    svc, repo, llm, _ = first_import({f"extract:{h2}": {"decisions": [
        extracted("API 規範", "API 欄位改 camelCase", [q(1, "那就延到 11/1 吧")])]}})
    api, timeline = repo.find_topic_by_name("API 規範"), repo.find_topic_by_name("v2 上線時程")
    with repo.unit_of_work() as w:
        w.merge_topic(api.id, timeline.id)
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)
    assert repo.decisions[report.created[0]].topic_id == timeline.id
    assert [t.id for t in repo.list_topics()] == [timeline.id] and report.topics_created == 0
    prompt = [p for m, _, p in llm.calls if m == "ExtractionResult"][-1]
    assert "API 規範" not in prompt.split("EXISTING TOPICS:")[1].splitlines()[0]


def test_an_emptied_topic_reappears_when_a_new_decision_is_assigned_to_it():
    """Moves can empty a topic; it stays live, so exact-name matches and the LLM can still choose it."""
    from decision_ledger.services.topics import TopicService

    h2, h3 = content_hash(T2), content_hash(T3)
    svc, repo, llm, first = first_import({
        f"extract:{h2}": {"decisions": [extracted("API 規範", "API 欄位改 camelCase", [q(1, "那就延到 11/1 吧")])]},
    })
    api, timeline = repo.find_topic_by_name("API 規範"), repo.find_topic_by_name("v2 上線時程")
    TopicService(repo).move(api.id, [first.created[1]], timeline.id)  # API 規範 is now empty
    report = svc.ingest(SourceKind.TRANSCRIPT, "t2", T2)  # exact-name short-circuit
    assert repo.decisions[report.created[0]].topic_id == api.id
    TopicService(repo).move(api.id, [report.created[0]], timeline.id)
    llm.responses.update({
        f"extract:{h3}": {"decisions": [extracted("介面命名", "回應欄位用 snake_case", [q(0, "QA 新人這週報到了")])]},
        f"topic:{h3}": {"assignments": [{"index": 0, "topic_id": api.id}]},
    })
    again = svc.ingest(SourceKind.TRANSCRIPT, "t3", T3)  # the LLM may pick the emptied topic
    assert repo.decisions[again.created[0]].topic_id == api.id
