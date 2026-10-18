from decision_ledger.domain.models import DecisionStatus
from decision_ledger.services.drafts import DecisionDraft, Relation, VerifiedQuote
from decision_ledger.services.reconciliation import ReconciliationService, same_topic
from tests.factories import decision
from tests.fakes import FakeLLM


def draft(index=0, topic="v2 上線時程", statement="v2 上線延到 11/1"):
    q = VerifiedQuote(segment_index=0, locator="00:12:34", speaker="Alice", quote="那就延到 11/1 吧")
    return DecisionDraft(index=index, topic=topic, statement=statement, rationale="QA 不足", decided_by="Alice", evidence=[q])


def reconcile(llm, drafts, existing, topic_of=None):
    """Every draft goes to topic T1 (the factory default) unless told otherwise."""
    topic_of = {d.index: "T1" for d in drafts} if topic_of is None else topic_of
    return ReconciliationService(llm).reconcile(drafts, existing, content_hash="h", topic_of=topic_of)


def test_no_same_topic_candidate_is_new_without_llm_call():
    llm = FakeLLM()
    out = reconcile(llm, [draft(topic="前端技術選型")], [decision("17")], topic_of={0: "T2"})
    assert out[0].relation is Relation.NEW and out[0].existing is None
    assert llm.calls == []


def test_new_topic_has_no_candidates():
    llm = FakeLLM()
    out = reconcile(llm, [draft()], [decision("17")], topic_of={0: None})
    assert out[0].relation is Relation.NEW and llm.calls == []


def test_candidates_come_from_the_assigned_topic_not_the_wording():
    # "資料庫選型" was assigned to the topic of decision #17 even though the text differs.
    llm = FakeLLM({"reconcile:h:0": {"relation": "supersede", "existing_id": "17"}})
    existing = [decision("17", topic="DB 選擇", topic_id="T7"), decision("18", topic="資料庫選型", topic_id="T8")]
    out = reconcile(llm, [draft(topic="資料庫選型")], existing, topic_of={0: "T7"})
    assert (out[0].relation, out[0].existing.id) == (Relation.SUPERSEDE, "17")
    assert "#17" in llm.calls[0][2] and "#18" not in llm.calls[0][2]


def test_llm_judgement_is_used_for_same_topic_candidates():
    llm = FakeLLM({"reconcile:h:0": {"relation": "supersede", "existing_id": "17", "explanation": "結論更新"}})
    out = reconcile(llm, [draft()], [decision("17")])
    assert out[0].relation is Relation.SUPERSEDE
    assert out[0].existing.id == "17"
    assert "#17" in llm.calls[0][2] and "v2 上線延到 11/1" in llm.calls[0][2]


def test_merge_and_conflict_relations_carry_existing_decision():
    llm = FakeLLM({
        "reconcile:h:0": {"relation": "merge", "existing_id": "42"},
        "reconcile:h:1": {"relation": "conflict", "existing_id": "42"},
    })
    existing = decision("42", statement="v2 上線延到 11/1")
    out = reconcile(llm, [draft(0), draft(1, statement="照原計畫 10/15")], [existing])
    assert [(o.relation, o.existing.id) for o in out] == [(Relation.MERGE, "42"), (Relation.CONFLICT, "42")]


def test_unknown_existing_id_falls_back_to_new():
    llm = FakeLLM({"reconcile:h:0": {"relation": "conflict", "existing_id": "999"}})
    out = reconcile(llm, [draft()], [decision("17")])
    assert out[0].relation is Relation.NEW and out[0].existing is None


def test_superseded_decisions_are_not_candidates():
    llm = FakeLLM()
    old = decision("17", status=DecisionStatus.SUPERSEDED)
    out = reconcile(llm, [draft()], [old])
    assert out[0].relation is Relation.NEW and llm.calls == []


def test_same_topic_ignores_case_width_and_spacing():
    assert same_topic("V2 上線時程", "v2上線時程")
    assert same_topic("ＡＰＩ 規範", "api規範")
    assert not same_topic("API 規範", "服務架構")


def test_existing_id_with_hash_still_resolves():
    """Gemini answered "#17" in the recorded demo; a merge must not silently become a new decision."""
    llm = FakeLLM({"reconcile:h:0": {"relation": "merge", "existing_id": "#17"}})
    out = reconcile(llm, [draft()], [decision("17")])
    assert out[0].relation is Relation.MERGE and out[0].existing.id == "17"
