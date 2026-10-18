"""scripts/record_demo.py: budgeted recording of the demo scenario (REQ-LEDGER-PLATFORM-006 AC2), no network."""

import importlib.util
import sys
from pathlib import Path

import pytest

from decision_ledger.adapters.llm.recording import RecordingBudgetExceeded

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("record_demo", ROOT / "scripts" / "record_demo.py")
record_demo = importlib.util.module_from_spec(_spec)
sys.modules["record_demo"] = record_demo
_spec.loader.exec_module(record_demo)


class EmptyLLM:
    """Stands in for Gemini: every call answers with an empty response."""

    name = "gemini"

    def __init__(self):
        self.calls = 0

    def generate_json(self, prompt, response_model, *, key):
        self.calls += 1
        return response_model()


def test_records_into_a_fresh_file_and_adds_calls_to_the_counter(tmp_path):
    out, counter = tmp_path / "demo.json", tmp_path / "calls.txt"
    out.write_text('{"stale": {}}', encoding="utf-8")
    counter.write_text("5\n", encoding="utf-8")
    inner = EmptyLLM()
    result = record_demo.record(inner, out_path=out, counter_path=counter, db_dir=tmp_path)
    assert inner.calls > 0 and result.calls == inner.calls
    assert record_demo.used_calls(counter) == 5 + inner.calls
    assert "stale" not in out.read_text(encoding="utf-8")  # the whole file is replaced, no orphan keys
    assert set(result.moments) == {"conflict", "assumption", "deviation"}


def test_budget_stops_mid_run_and_still_counts_the_calls(tmp_path):
    out, counter = tmp_path / "demo.json", tmp_path / "calls.txt"
    counter.write_text(f"{record_demo.BUDGET - 2}\n", encoding="utf-8")
    inner = EmptyLLM()
    with pytest.raises(RecordingBudgetExceeded):
        record_demo.record(inner, out_path=out, counter_path=counter, db_dir=tmp_path)
    assert inner.calls == 2 and record_demo.used_calls(counter) == record_demo.BUDGET
    assert not out.exists()  # a partial recording never replaces the fixture


def test_missing_counter_means_nothing_used(tmp_path):
    assert record_demo.used_calls(tmp_path / "none.txt") == 0


def test_require_all_keeps_the_current_fixture_when_a_moment_is_missing(tmp_path):
    out, counter = tmp_path / "demo.json", tmp_path / "calls.txt"
    out.write_text('{"keep": {}}', encoding="utf-8")
    result = record_demo.record(EmptyLLM(), out_path=out, counter_path=counter, db_dir=tmp_path, require_all=True)
    assert not result.replaced and "keep" in out.read_text(encoding="utf-8")
    assert (tmp_path / "demo.json.partial").exists() and record_demo.used_calls(counter) == result.calls


def test_settings_repr_never_shows_secrets():
    from decision_ledger.bootstrap import Settings

    text = repr(Settings.from_env({"DL_GEMINI_ACCESS_TOKEN": "tok-123", "GEMINI_API_KEY": "key-456", "DL_WEBHOOK_SECRET": "sec-789"}))
    assert "tok-123" not in text and "key-456" not in text and "sec-789" not in text


def test_resume_reuses_a_partial_recording_without_new_calls(tmp_path):
    out, counter = tmp_path / "demo.json", tmp_path / "calls.txt"
    first = EmptyLLM()
    record_demo.record(first, out_path=out, counter_path=counter, db_dir=tmp_path)
    out.rename(tmp_path / "demo.json.partial")  # pretend the run stopped after recording everything
    second = EmptyLLM()
    result = record_demo.record(second, out_path=out, counter_path=counter, db_dir=tmp_path, resume=True)
    assert second.calls == 0 and result.calls == 0 and out.exists()


def test_write_notes_records_model_budget_and_moments(tmp_path):
    from decision_ledger.bootstrap import Settings

    settings = Settings.from_env({"DL_LLM_BACKEND": "gemini", "DL_GEMINI_MODEL": "gemini-3.5-flash", "GOOGLE_CLOUD_LOCATION": "global",
                                  "DL_GEMINI_ACCESS_TOKEN": "tok-secret"})
    result = record_demo.RecordResult(calls=3, summary={"decisions": 1}, moments={"conflict": True}, pr_violated={87: True})
    notes = tmp_path / "RECORDING.md"
    record_demo.write_notes(result, settings, total_used=10, path=notes)
    text = notes.read_text(encoding="utf-8")
    assert "gemini-3.5-flash" in text and "global" in text and f"10 of {record_demo.BUDGET}" in text and '"87": true' in text
    assert "tok-secret" not in text


def test_main_refuses_the_fake_backend(monkeypatch, capsys):
    monkeypatch.setenv("DL_LLM_BACKEND", "fake")
    assert record_demo.main([]) == 2
    assert "real backend" in capsys.readouterr().err


def test_main_exit_code_reflects_the_demo_moments(monkeypatch, tmp_path):
    monkeypatch.setenv("DL_LLM_BACKEND", "gemini")
    monkeypatch.setattr(record_demo, "build_llm", lambda settings: EmptyLLM())
    monkeypatch.setattr(record_demo, "OUT_PATH", tmp_path / "demo.json")
    calls = []
    real_record = record_demo.record
    monkeypatch.setattr(record_demo, "record", lambda inner, **kw: calls.append(kw) or real_record(inner, out_path=tmp_path / "demo.json", **kw))
    monkeypatch.setattr(record_demo, "write_notes", lambda *a, **k: None)
    assert record_demo.main(["--counter", str(tmp_path / "calls.txt")]) == 1  # empty responses: no demo moment
    assert calls and calls[0]["counter_path"] == tmp_path / "calls.txt"
