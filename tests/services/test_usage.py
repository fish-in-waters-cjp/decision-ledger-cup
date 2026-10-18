"""DL-021: daily LLM call limits per sandbox (project) and site-wide."""

import pytest
from pydantic import BaseModel

from decision_ledger.domain.errors import UsageLimitExceeded
from decision_ledger.services.structured import call_structured
from decision_ledger.services.usage import LimitedLLM, usage_day
from tests.fakes import FakeLLM, FakeUsageCounter


class Out(BaseModel):
    value: int = 0


def limited(counter, project="P1", sandbox=3, site=5, day="2026-10-17", inner=None):
    inner = inner or FakeLLM({"Out": {"value": 1}})
    return LimitedLLM(inner, counter, project_id=project, sandbox_limit=sandbox, site_limit=site, today=lambda: day), inner


def test_calls_within_the_sandbox_limit_reach_the_model():
    llm, inner = limited(FakeUsageCounter())
    for _ in range(3):
        assert llm.generate_json("p", Out, key="k").value == 1
    assert len(inner.calls) == 3 and llm.name == inner.name


def test_call_over_the_sandbox_limit_is_refused_without_calling_the_model():
    counter = FakeUsageCounter()
    llm, inner = limited(counter)
    for _ in range(3):
        llm.generate_json("p", Out, key="k")
    with pytest.raises(UsageLimitExceeded) as info:
        llm.generate_json("p", Out, key="k")
    assert info.value.scope == "sandbox" and len(inner.calls) == 3


def test_site_limit_is_shared_across_projects():
    counter = FakeUsageCounter()
    a, inner_a = limited(counter, project="P1", sandbox=10, site=4)
    b, inner_b = limited(counter, project="P2", sandbox=10, site=4)
    for _ in range(2):
        a.generate_json("p", Out, key="k")
        b.generate_json("p", Out, key="k")
    with pytest.raises(UsageLimitExceeded) as info:
        b.generate_json("p", Out, key="k")
    assert info.value.scope == "site" and len(inner_a.calls) + len(inner_b.calls) == 4


def test_counts_restart_on_a_new_utc_day():
    counter = FakeUsageCounter()
    day = {"d": "2026-10-17"}
    inner = FakeLLM({"Out": {"value": 1}})
    llm = LimitedLLM(inner, counter, project_id="P1", sandbox_limit=1, site_limit=10, today=lambda: day["d"])
    llm.generate_json("p", Out, key="k")
    day["d"] = "2026-10-18"
    llm.generate_json("p", Out, key="k")
    assert len(inner.calls) == 2


def test_call_structured_does_not_retry_a_limit_refusal():
    counter = FakeUsageCounter()
    llm, inner = limited(counter, sandbox=0)
    with pytest.raises(UsageLimitExceeded):
        call_structured(llm, "p", Out, key="k")
    assert inner.calls == [] and counter.get("sandbox", "P1:2026-10-17") == 1


def test_usage_day_is_a_utc_date():
    assert len(usage_day()) == 10 and usage_day()[4] == "-"



def test_a_sandbox_over_its_limit_does_not_consume_site_quota():
    """DL-021 review F-2: the sandbox check runs first, so refused calls leave the site count alone."""
    counter = FakeUsageCounter()
    llm, inner = limited(counter, sandbox=1, site=100)
    llm.generate_json("p", Out, key="k")
    for _ in range(3):
        with pytest.raises(UsageLimitExceeded):
            llm.generate_json("p", Out, key="k")
    assert counter.get("site", "2026-10-17") == 1 and len(inner.calls) == 1


def test_no_sandbox_limit_counts_the_call_site_wide_only():
    counter = FakeUsageCounter()
    llm, inner = limited(counter, sandbox=None, site=2)
    llm.generate_json("p", Out, key="k")
    llm.generate_json("p", Out, key="k")
    with pytest.raises(UsageLimitExceeded) as info:
        llm.generate_json("p", Out, key="k")
    assert info.value.scope == "site" and len(inner.calls) == 2
    assert counter.get("sandbox", "P1:2026-10-17") == 0 and counter.get("site", "2026-10-17") == 3
