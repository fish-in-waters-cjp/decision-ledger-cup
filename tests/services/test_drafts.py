import pytest

from decision_ledger.domain.models import Segment
from decision_ledger.services.drafts import QuoteRef, VerifiedQuote, decision_ref, render_segments, verify_quotes

SEGS = [
    Segment(index=0, locator="00:12:02", speaker="Carol", text="QA 現在只有兩個人，回歸測試一定做不完。"),
    Segment(index=1, locator="00:12:34", speaker="Alice", text="那就延到 11/1 吧，品質比較重要。"),
]


def test_quote_found_in_referenced_segment():
    [v] = verify_quotes([QuoteRef(segment_index=1, quote="延到 11/1")], SEGS)
    assert (v.locator, v.speaker, v.segment_index) == ("00:12:34", "Alice", 1)


def test_wrong_index_falls_back_to_any_segment():
    [v] = verify_quotes([QuoteRef(segment_index=1, quote="QA 現在只有兩個人")], SEGS)
    assert v.segment_index == 0


def test_normalises_width_whitespace_and_quote_marks():
    assert verify_quotes([QuoteRef(segment_index=1, quote="「那就延到１１/１吧」")], SEGS)


def test_unmatched_or_empty_quotes_are_dropped():
    assert verify_quotes([QuoteRef(segment_index=0, quote="不存在"), QuoteRef(segment_index=0, quote="  ")], SEGS) == []


def test_to_evidence_and_render_segments():
    ev = VerifiedQuote(segment_index=0, locator="L", speaker="S", quote="q").to_evidence("7")
    assert (ev.source_id, ev.locator, ev.quote) == ("7", "L", "q")
    assert render_segments(SEGS).splitlines()[0] == "[0] (00:12:02) Carol: QA 現在只有兩個人，回歸測試一定做不完。"


@pytest.mark.parametrize("raw, expected", [
    ("4", "4"), ("#4", "4"), (" decision #4 ", "4"), ("Decision 12", "12"), ("#T3", "T3"), ("", ""),
])
def test_decision_ref_accepts_the_forms_models_return(raw, expected):
    """Recorded Gemini output referred to decisions as "#4" and "decision #4"; both mean id 4."""
    assert decision_ref(raw) == expected
