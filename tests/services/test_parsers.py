import json

import pytest

from decision_ledger.domain.errors import ParseError
from decision_ledger.services.parsers import content_hash, parse_slack, parse_source, parse_transcript
from decision_ledger.domain.models import SourceKind

TRANSCRIPT = """
[00:08:02] Alice: 客戶的年度採購季在 10 月下旬。
[00:08:12] Alice: 那 v2 就定 10/15 上線。
  補充：要趕在採購季之前。
[11:30] Dave: API 回傳欄位統一用 snake_case。
"""


def test_parse_transcript_uses_timestamp_as_locator():
    segs = parse_transcript(TRANSCRIPT)
    assert [s.locator for s in segs] == ["00:08:02", "00:08:12", "11:30"]
    assert [s.speaker for s in segs] == ["Alice", "Alice", "Dave"]
    assert [s.index for s in segs] == [0, 1, 2]


def test_parse_transcript_joins_continuation_lines():
    segs = parse_transcript(TRANSCRIPT)
    assert segs[1].text == "那 v2 就定 10/15 上線。 補充：要趕在採購季之前。"


def test_parse_transcript_accepts_fullwidth_colon():
    segs = parse_transcript("[00:01:00] Carol：QA 只有兩個人")
    assert segs[0].speaker == "Carol" and segs[0].text == "QA 只有兩個人"


@pytest.mark.parametrize("text", ["", "   \n", "沒有時間戳的一行\n[00:01:00] A: b"])
def test_parse_transcript_rejects_unparseable_input(text):
    with pytest.raises(ParseError):
        parse_transcript(text)


def test_parse_slack_uses_message_ts_and_speaker():
    data = [
        {"ts": "1726452120.000100", "user": "U1", "user_profile": {"real_name": "Alice"}, "text": "v2 上線日改為 11/1"},
        {"ts": "1726816271.000200", "user_name": "Frank", "text": "對，10/15 上"},
        {"ts": "1726816272.000300", "user": "U3", "subtype": "channel_join", "text": "joined"},
        {"ts": "1726816273.000400", "user": "U4", "text": "   "},
    ]
    segs = parse_slack(json.dumps(data))
    assert [(s.locator, s.speaker) for s in segs] == [("1726452120.000100", "Alice"), ("1726816271.000200", "Frank")]
    assert segs[0].text == "v2 上線日改為 11/1"


def test_parse_slack_accepts_messages_wrapper():
    segs = parse_slack(json.dumps({"messages": [{"ts": "1.0", "user": "U1", "text": "hi"}]}))
    assert segs[0].speaker == "U1"


@pytest.mark.parametrize("raw", ["not json", "{}", "[]", '[{"text": "no ts"}]', '"x"'])
def test_parse_slack_rejects_invalid_input(raw):
    with pytest.raises(ParseError):
        parse_slack(raw)


def test_parse_source_dispatches_by_kind():
    assert parse_source(SourceKind.TRANSCRIPT, "[00:00:01] A: b")[0].locator == "00:00:01"
    assert parse_source(SourceKind.SLACK, '[{"ts": "1.0", "user": "U", "text": "t"}]')[0].locator == "1.0"


def test_content_hash_is_stable_and_whitespace_insensitive_at_edges():
    assert content_hash("abc\n") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


def test_parse_slack_tolerates_non_string_fields():
    """Verify 3/5 (R2-2): odd field types are coerced instead of crashing with 500."""
    segs = parse_slack(json.dumps([
        {"ts": 1726452120.0001, "user": 123, "text": "hi"},
        {"ts": "2.0", "user_profile": "not-a-dict", "user_name": ["x"], "text": 42},
    ]))
    assert [(s.locator, s.speaker, s.text) for s in segs] == [("1726452120.0001", "123", "hi"), ("2.0", "['x']", "42")]
