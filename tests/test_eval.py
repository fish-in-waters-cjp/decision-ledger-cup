"""scripts/eval.py: scoring against gold answers (REQ-LEDGER-PLATFORM-008), no network."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from decision_ledger.adapters.llm.fake import FakeReplayLLM
from decision_ledger.domain.models import AlertKind, Segment, Source, SourceKind
from decision_ledger.services.drafts import ExtractionResult
from tests.factories import alert, decision, ev
from tests.fakes import FakeLedgerStore

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("dl_eval", ROOT / "scripts" / "eval.py")
dl_eval = importlib.util.module_from_spec(_spec)
sys.modules["dl_eval"] = dl_eval
_spec.loader.exec_module(dl_eval)

MANIFEST = {"steps": [
    {"type": "source", "file": "a.txt", "title": "Kickoff"},
    {"type": "source", "file": "b.txt", "title": "Weekly"},
]}
GOLD = {
    "decisions": [
        {"id": "D1", "source": "a.txt", "keywords": ["guest"]},
        {"id": "D2", "source": "a.txt", "keywords": ["postgres"]},
        {"id": "D3", "source": "b.txt", "keywords": ["account"]},
    ],
    "conflicts": [["D1", "D3"]],
    "supersessions": [],
    "deviations": [],
    "premise_changes": [{"decision": "D1", "premise_keywords": ["two testers"], "evidence_keywords": ["four testers"], "source": "b.txt"}],
    "premise_negatives": [{"quote": "The mobile team has four testers now", "source": "b.txt"}],
}


class Inner:
    name = "gemini"

    def __init__(self, responses=None):
        self.responses = responses or {}

    def generate_json(self, prompt, response_model, *, key):
        return response_model.model_validate(self.responses.get(key, {}))


def build_run(decisions, alerts=(), responses=None):
    store = FakeLedgerStore()
    store.create_project("P", None)
    repo = store.for_project("P1")
    repo.sources["S1"] = Source(id="S1", kind=SourceKind.TRANSCRIPT, title="Kickoff", content_hash="h1",
                                segments=[Segment(index=0, locator="00:00:01", speaker="A", text="Let's allow guest checkout.")])
    repo.sources["S2"] = Source(id="S2", kind=SourceKind.TRANSCRIPT, title="Weekly", content_hash="h2")
    for d in decisions:
        repo.decisions[d.id] = d
    for a in alerts:
        repo.alerts[a.id] = a
    llm = dl_eval.CapturingLLM(Inner())
    for key, value in (responses or {}).items():
        llm.responses[key] = value
    return dl_eval.Run(repo=repo, manifest=MANIFEST, llm=llm)


def d(id_, statement, source="S1"):
    return decision(id_, statement=statement).model_copy(update={"evidence": [ev("q", source_id=source)]})


def test_decisions_match_one_to_one_and_duplicates_are_false_positives():
    run = build_run([d("1", "Allow guest checkout"), d("2", "Guest checkout is allowed"), d("3", "Every order needs an account", "S2")])
    metrics = dl_eval.score(run, GOLD)
    c = metrics.counts["extraction"]
    assert (c.hit, c.fp, c.miss) == (2, 1, 1)  # D1 and D3 matched, the restatement is FP, D2 (postgres) missed


def test_conflict_premise_and_empty_tasks():
    decisions = [d("1", "Allow guest checkout"), d("3", "Every order needs an account", "S2")]
    alerts = [
        alert("1", AlertKind.CONFLICT, ["1", "3"]),
        alert("2", AlertKind.ASSUMPTION, ["1"], assumption_text="QA has two testers", evidence=[ev("QA is four testers now", source_id="S2")]),
        alert("3", AlertKind.ASSUMPTION, ["3"], assumption_text="Something else", evidence=[ev("y", source_id="S2")]),
    ]
    metrics = dl_eval.score(build_run(decisions, alerts), GOLD)
    assert (metrics.counts["conflict"].hit, metrics.counts["conflict"].fp, metrics.counts["conflict"].miss) == (1, 0, 0)
    assert (metrics.counts["premise"].hit, metrics.counts["premise"].fp, metrics.counts["premise"].miss) == (1, 1, 0)
    dev = metrics.counts["deviation"]
    assert (dev.hit, dev.fp, dev.miss) == (0, 0, 0) and dev.precision == "N/A" and dev.recall == "N/A"


def test_quote_rate_reuses_ingestion_verification_and_counts_raw_model_quotes():
    raw = ExtractionResult.model_validate({"decisions": [
        {"topic": "t", "statement": "s", "evidence": [{"segment_index": 0, "quote": "allow guest checkout"},
                                                       {"segment_index": 0, "quote": "an invented sentence"}]},
    ]})
    run = build_run([d("1", "Allow guest checkout")], responses={"extract:h1": raw})
    assert dl_eval.quote_rate(run) == (1, 2)
    empty = dl_eval.score(build_run([]), {"decisions": []})
    assert (empty.quotes_verbatim, empty.quotes_total, empty.quote_rate) == (0, 0, "N/A")


class Out(BaseModel):
    value: int = 0


def test_capturing_llm_budget_replay_misses_and_last_response_wins(tmp_path):
    (tmp_path / "r.json").write_text(json.dumps({"k": {"value": 1}}), encoding="utf-8")
    llm = dl_eval.CapturingLLM(FakeReplayLLM(tmp_path))
    llm.generate_json("p", Out, key="k")
    llm.generate_json("p", Out, key="missing")
    assert llm.replay_misses == 1 and llm.responses["k"] == Out(value=1)

    class Flaky:
        name = "gemini"

        def __init__(self):
            self.results = [ValueError("invalid"), Out(value=7)]

        def generate_json(self, prompt, response_model, *, key):
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    retrying = dl_eval.CapturingLLM(Flaky(), max_calls=2)
    with pytest.raises(ValueError):
        retrying.generate_json("p", Out, key="r")
    assert "r" not in retrying.responses  # a failed attempt leaves nothing to count
    retrying.generate_json("p", Out, key="r")
    assert retrying.responses["r"] == Out(value=7)
    with pytest.raises(dl_eval.EvalBudgetExceeded):
        retrying.generate_json("p", Out, key="r")


def test_run_scenario_replays_the_demo_with_dry_run_comments():
    from decision_ledger.bootstrap import DEMO_DIR

    run = dl_eval.run_scenario(FakeReplayLLM(DEMO_DIR / "llm"))
    assert run.repo.list_decisions() and run.llm.replay_misses == 0
    assert all(c.comment_published in (None, False) for c in run.repo.list_pr_checks())


def test_render_prints_integers_and_na():
    metrics = dl_eval.Metrics(counts={t: dl_eval.Count() for t in dl_eval.TASKS}, quotes_verbatim=0, quotes_total=0)
    metrics.counts["extraction"] = dl_eval.Count(hit=5, fp=1, miss=1)
    text = dl_eval.render(metrics, ["header"])
    assert text.splitlines()[0] == "header"
    assert any(line.split()[:4] == ["extraction", "5", "1", "1"] and "0.83" in line for line in text.splitlines())
    assert any(line.startswith("conflict") and line.split()[-2:] == ["N/A", "N/A"] for line in text.splitlines())
    assert "verbatim quotes: 0/0 (N/A)" in text


def test_main_writes_the_report_and_stops_on_budget(tmp_path, monkeypatch, capsys):
    out = tmp_path / "results.txt"
    monkeypatch.delenv("DL_LLM_BACKEND", raising=False)
    assert dl_eval.main(["--out", str(out)]) == 0
    assert "extraction" in out.read_text(encoding="utf-8") and "replay of responses recorded from Gemini" in out.read_text(encoding="utf-8")

    def broke(*a, **k):
        raise dl_eval.EvalBudgetExceeded("used up")

    monkeypatch.setattr(dl_eval, "run_scenario", broke)
    late = tmp_path / "late.txt"
    assert dl_eval.main(["--out", str(late)]) == 3
    assert "incomplete" in capsys.readouterr().out and not late.exists()


def test_gold_files_live_outside_the_runtime_package():
    assert dl_eval.DEFAULT_GOLD.is_file()
    assert (ROOT / "src") not in dl_eval.DEFAULT_GOLD.resolve().parents


def test_repeated_predictions_of_one_gold_pair_are_false_positives():
    """Review R1-01: two conflict alerts on the same pair give one hit and one FP."""
    decisions = [d("1", "Allow guest checkout"), d("3", "Every order needs an account", "S2")]
    alerts = [alert("1", AlertKind.CONFLICT, ["1", "3"]), alert("2", AlertKind.CONFLICT, ["3", "1"])]
    c = dl_eval.score(build_run(decisions, alerts), GOLD).counts["conflict"]
    assert (c.hit, c.fp, c.miss) == (1, 1, 0)


def test_premise_needs_the_change_in_the_new_evidence_and_ignores_planted_negatives():
    """Review R1-02: a premise alert backed by a negative (another team's tester) is a false positive."""
    decisions = [d("1", "Allow guest checkout")]
    trap = alert("1", AlertKind.ASSUMPTION, ["1"], assumption_text="QA has two testers",
                 evidence=[ev("The mobile team has four testers now", source_id="S2")])
    wrong_source = alert("2", AlertKind.ASSUMPTION, ["1"], assumption_text="QA has two testers",
                         evidence=[ev("QA is four testers now", source_id="S1")])
    c = dl_eval.score(build_run(decisions, [trap, wrong_source]), GOLD).counts["premise"]
    assert (c.hit, c.fp, c.miss) == (0, 2, 1)


def test_deviation_and_supersession_scoring():
    """Review R1-03 and R1-07: deviations key on (PR number, decision); supersession keeps its direction."""
    from decision_ledger.domain.models import PrCheck

    gold = {**GOLD, "deviations": [{"pr": 87, "decision": "D2"}], "supersessions": [{"old": "D1", "new": "D2"}]}
    decisions = [d("1", "Allow guest checkout"), d("2", "Orders stay on Postgres")]
    alerts = [
        alert("1", AlertKind.DEVIATION, ["2"], pr_check_key="k87"),
        alert("2", AlertKind.SUPERSESSION, ["1", "2"], superseded_id="2", superseding_id="1"),  # wrong direction
    ]
    run = build_run(decisions, alerts)
    run.repo.pr_checks["k87"] = PrCheck(key="k87", repo="r", pr_number=87, head_sha="s", diff="", comment="", violated=True)
    metrics = dl_eval.score(run, gold)
    dev, sup = metrics.counts["deviation"], metrics.supersession
    assert (dev.hit, dev.fp, dev.miss) == (1, 0, 0)
    assert (sup.hit, sup.fp, sup.miss) == (0, 1, 1)


def test_keywords_match_whole_words():
    """Review R1-08: "november 3" must not match "november 30"."""
    assert dl_eval.has_keywords("Launch on November 3.", ["november 3"])
    assert not dl_eval.has_keywords("Launch on November 30.", ["november 3"])
    assert dl_eval.has_keywords("Nothing under src/payments/ imports it", ["src/payments"])
    assert not dl_eval.has_keywords("Nothing under mysrc/payments/ imports it", ["src/payments"])
    assert not dl_eval.has_keywords("lib/src/payments is vendored", ["src/payments"])


def test_main_ignores_the_record_path_and_refuses_partial_replays(tmp_path, monkeypatch, capsys):
    """Review R1-04 and R1-09: evaluation never records, and missing replay keys mean no results file."""
    rec = tmp_path / "rec.json"
    monkeypatch.setenv("DL_LLM_RECORD_PATH", str(rec))
    dl_eval.main([])
    assert not rec.exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("DL_FIXTURES_DIR", str(empty))  # a Fake backend with nothing recorded
    out = tmp_path / "r.txt"
    assert dl_eval.main(["--out", str(out)]) == 4 and not out.exists()
    assert "incomplete" in capsys.readouterr().out


def test_real_backends_charge_the_shared_call_ledger(tmp_path, monkeypatch):
    """Review R1-05: a live evaluation is capped by, and recorded in, the recording budget."""
    counter = tmp_path / "calls.txt"
    counter.write_text(f"{dl_eval.BUDGET - 3}\n", encoding="utf-8")

    class Live:
        name = "gemini"

        def generate_json(self, prompt, response_model, *, key):
            return response_model()

    monkeypatch.setenv("DL_LLM_BACKEND", "gemini")
    monkeypatch.setattr(dl_eval, "build_llm", lambda settings: Live())
    assert dl_eval.main(["--counter", str(counter)]) == 3  # the demo needs more than the 3 calls left
    assert dl_eval.used_calls(counter) == dl_eval.BUDGET


def test_calls_are_charged_when_a_live_run_fails_for_any_reason(tmp_path, monkeypatch):
    """Review R2-01: a 504 or an invalid response still costs money, so it is charged."""
    counter = tmp_path / "calls.txt"

    class Failing:
        name = "gemini"

        def __init__(self):
            self.calls = 0

        def generate_json(self, prompt, response_model, *, key):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("504 DEADLINE_EXCEEDED")
            return response_model()

    monkeypatch.setenv("DL_LLM_BACKEND", "gemini")
    monkeypatch.setattr(dl_eval, "build_llm", lambda settings: Failing())
    with pytest.raises(RuntimeError):
        dl_eval.main(["--counter", str(counter)])
    assert dl_eval.used_calls(counter) == 3


def test_premise_evidence_must_come_from_that_gold_items_own_source():
    """Review R2-03: with two gold premise changes, evidence from the other one's source does not count."""
    gold = {**GOLD, "premise_changes": [
        {"decision": "D1", "premise_keywords": ["two testers"], "evidence_keywords": ["four testers"], "source": "b.txt"},
        {"decision": "D3", "premise_keywords": ["login"], "evidence_keywords": ["no login"], "source": "a.txt"},
    ]}
    decisions = [d("1", "Allow guest checkout")]
    misplaced = alert("1", AlertKind.ASSUMPTION, ["1"], assumption_text="QA has two testers",
                      evidence=[ev("QA is four testers now", source_id="S1")])  # S1 is a.txt, not D1's b.txt
    c = dl_eval.score(build_run(decisions, [misplaced]), gold).counts["premise"]
    assert (c.hit, c.fp) == (0, 1)


def test_main_succeeds_on_the_full_recording(monkeypatch):
    monkeypatch.delenv("DL_LLM_RECORD_PATH", raising=False)
    monkeypatch.delenv("DL_LLM_BACKEND", raising=False)
    assert dl_eval.main([]) == 0
