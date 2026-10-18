import pytest
from pydantic import BaseModel

from decision_ledger.domain.errors import LLMOutputError, LLMTransportError, LLMUnavailableError
from decision_ledger.services.structured import call_structured
from tests.fakes import FakeLLM


class Answer(BaseModel):
    value: int


def test_returns_validated_model():
    llm = FakeLLM({"k": {"value": 3}})
    assert call_structured(llm, "p", Answer, key="k").value == 3


def test_retries_once_after_invalid_output():
    llm = FakeLLM({"k": [{"value": "not-int"}, {"value": 4}]})
    assert call_structured(llm, "p", Answer, key="k").value == 4
    assert len(llm.calls) == 2


def test_retries_once_after_transport_error():
    llm = FakeLLM({"k": [LLMTransportError("timeout"), {"value": 5}]})
    assert call_structured(llm, "p", Answer, key="k").value == 5


def test_raises_after_second_failure():
    llm = FakeLLM({"k": [{"value": "x"}, {"value": "y"}]})
    with pytest.raises(LLMOutputError):
        call_structured(llm, "p", Answer, key="k")
    assert len(llm.calls) == 2


def test_rejects_wrong_model_instance():
    class Other(BaseModel):
        value: int = 1

    llm = FakeLLM({"k": [Other(), Other()]})
    with pytest.raises(LLMOutputError):
        call_structured(llm, "p", Answer, key="k")



# --- DL-022: transport failures are reported as unavailable, not invalid output ---

def test_two_transport_failures_raise_unavailable():
    llm = FakeLLM({"k": [LLMTransportError("exit 1"), LLMTransportError("exit 1")]})
    with pytest.raises(LLMUnavailableError):
        call_structured(llm, "p", Answer, key="k")


def test_two_invalid_outputs_are_not_unavailable():
    llm = FakeLLM({"k": [{"value": "x"}, {"value": "y"}]})
    with pytest.raises(LLMOutputError) as info:
        call_structured(llm, "p", Answer, key="k")
    assert not isinstance(info.value, LLMUnavailableError)


@pytest.mark.parametrize("first,second,unavailable", [
    (LLMTransportError("timeout"), {"value": "x"}, False),
    ({"value": "x"}, LLMTransportError("timeout"), True),
])
def test_mixed_failures_follow_the_last_attempt(first, second, unavailable):
    llm = FakeLLM({"k": [first, second]})
    with pytest.raises(LLMOutputError) as info:
        call_structured(llm, "p", Answer, key="k")
    assert isinstance(info.value, LLMUnavailableError) is unavailable
