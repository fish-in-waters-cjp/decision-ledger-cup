"""RecordingLLM: real responses saved in the Fake replay format (REQ-LEDGER-PLATFORM-005)."""

import json

import pytest
from pydantic import BaseModel

from decision_ledger.adapters.llm.fake import FakeReplayLLM
from decision_ledger.adapters.llm.recording import RecordingBudgetExceeded, RecordingLLM
from decision_ledger.domain.errors import LLMTransportError


class Out(BaseModel):
    value: int = 0
    items: list[str] = []


class Inner:
    name = "gemini"

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def generate_json(self, prompt, response_model, *, key):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_recorded_file_replays_with_the_fake_backend(tmp_path):
    path = tmp_path / "llm" / "demo.json"
    rec = RecordingLLM(Inner(Out(value=3, items=["a"]), {"value": 4}), path)
    assert rec.generate_json("p", Out, key="k1") == Out(value=3, items=["a"])
    assert rec.generate_json("p", Out, key="k2") == Out(value=4)  # a plain dict is validated too
    replay = FakeReplayLLM(path.parent)
    assert replay.generate_json("other prompt", Out, key="k1") == Out(value=3, items=["a"])
    assert replay.generate_json("p", Out, key="k2") == Out(value=4)
    assert not list(path.parent.glob("*.tmp"))  # written through a temp file and renamed


def test_existing_keys_are_kept_and_repeated_keys_overwritten(tmp_path):
    path = tmp_path / "demo.json"
    path.write_text(json.dumps({"old": {"value": 1}, "k": {"value": 2}}), encoding="utf-8")
    RecordingLLM(Inner(Out(value=9)), path).generate_json("p", Out, key="k")
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": {"value": 1}, "k": {"value": 9, "items": []}}


def test_name_is_forwarded_and_calls_count_failures(tmp_path):
    inner = Inner(LLMTransportError("boom"), Out(value=1))
    rec = RecordingLLM(inner, tmp_path / "demo.json")
    assert rec.name == "gemini"
    with pytest.raises(LLMTransportError):
        rec.generate_json("p", Out, key="k")
    assert rec.calls == 1 and not (tmp_path / "demo.json").exists()  # a failed call records nothing
    rec.generate_json("p", Out, key="k")
    assert rec.calls == 2


def test_budget_stops_before_calling_the_inner_backend(tmp_path):
    inner = Inner(Out(value=1), Out(value=2))
    rec = RecordingLLM(inner, tmp_path / "demo.json", max_calls=1)
    rec.generate_json("p", Out, key="a")
    with pytest.raises(RecordingBudgetExceeded):
        rec.generate_json("p", Out, key="b")
    assert inner.calls == 1 and rec.calls == 1
