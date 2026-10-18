from decision_ledger.domain.models import DecisionStatus, Segment
from decision_ledger.services.assumption_watch import AssumptionWatchService
from tests.factories import decision
from tests.fakes import FakeLLM

SEGMENTS = [
    Segment(index=0, locator="00:04:40", speaker="Alice", text="Carol，QA 那邊狀況如何？"),
    Segment(index=1, locator="00:05:12", speaker="Carol", text="好消息，QA 新人這週報到了，現在有 3 個人。"),
]


def finding(decision_id="42", assumption="QA 團隊在 10 月底前只有 2 人", quote="QA 新人這週報到了"):
    return {"findings": [{"decision_id": decision_id, "assumption_text": assumption,
                          "evidence": [{"segment_index": 1, "quote": quote}], "explanation": "人數改變"}]}


def test_contradicted_assumption_yields_alert_draft_with_both_sides():
    llm = FakeLLM({"assumption:h": finding()})
    d = decision("42", assumptions=["QA 團隊在 10 月底前只有 2 人"])
    alerts, discarded = AssumptionWatchService(llm).check(SEGMENTS, [d], content_hash="h")
    assert discarded == []
    assert len(alerts) == 1
    a = alerts[0]
    assert a.decision.id == "42"
    assert a.assumption.text == "QA 團隊在 10 月底前只有 2 人"
    assert a.assumption.evidence  # original side
    assert a.new_evidence[0].locator == "00:05:12"  # new side


def test_no_assumptions_means_no_llm_call():
    llm = FakeLLM()
    alerts, _ = AssumptionWatchService(llm).check(SEGMENTS, [decision("1")], content_hash="h")
    assert alerts == [] and llm.calls == []


def test_unrelated_content_yields_no_alert():
    llm = FakeLLM({"assumption:h": {"findings": []}})
    d = decision("42", assumptions=["客戶採購季在 10 月下旬"])
    alerts, discarded = AssumptionWatchService(llm).check(SEGMENTS, [d], content_hash="h")
    assert alerts == [] and discarded == []


def test_finding_without_verifiable_quote_is_discarded():
    llm = FakeLLM({"assumption:h": finding(quote="完全不存在的句子")})
    d = decision("42", assumptions=["QA 團隊在 10 月底前只有 2 人"])
    alerts, discarded = AssumptionWatchService(llm).check(SEGMENTS, [d], content_hash="h")
    assert alerts == []
    assert discarded[0].kind == "assumption_alert"


def test_finding_for_unknown_decision_or_assumption_is_discarded():
    llm = FakeLLM({"assumption:h": [finding(decision_id="7")]})
    d = decision("42", assumptions=["QA 團隊在 10 月底前只有 2 人"])
    alerts, discarded = AssumptionWatchService(llm).check(SEGMENTS, [d], content_hash="h")
    assert alerts == [] and len(discarded) == 1

    llm = FakeLLM({"assumption:h": finding(assumption="別的前提")})
    alerts, discarded = AssumptionWatchService(llm).check(SEGMENTS, [d], content_hash="h")
    assert alerts == [] and len(discarded) == 1


def test_only_active_decisions_are_watched():
    llm = FakeLLM()
    d = decision("42", assumptions=["x"], status=DecisionStatus.SUPERSEDED)
    alerts, _ = AssumptionWatchService(llm).check(SEGMENTS, [d], content_hash="h")
    assert alerts == [] and llm.calls == []


def test_decision_reference_with_prefix_still_matches():
    """Gemini answered "decision #42" in the recorded demo; the finding must not be discarded."""
    llm = FakeLLM({"assumption:h": finding(decision_id="decision #42")})
    d = decision("42", assumptions=["QA 團隊在 10 月底前只有 2 人"])
    alerts, discarded = AssumptionWatchService(llm).check(SEGMENTS, [d], content_hash="h")
    assert discarded == [] and [a.decision.id for a in alerts] == ["42"]
