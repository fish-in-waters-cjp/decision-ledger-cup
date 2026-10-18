"""Evaluation write-up stays tied to the measured run (DL-015)."""

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "eval-results.md"
RESULTS = ROOT / "demo" / "eval" / "dev-results.txt"
RECORDED = ROOT / "src" / "decision_ledger" / "demo" / "fixtures" / "llm" / "demo.json"
DISCLAIMER = "Results describe this test set only; they do not establish production accuracy."

_spec = importlib.util.spec_from_file_location("check_no_cjk", ROOT / "scripts" / "check_no_cjk.py")
check_no_cjk = importlib.util.module_from_spec(_spec)
sys.modules["check_no_cjk"] = check_no_cjk
_spec.loader.exec_module(check_no_cjk)


NAMES = {"extraction": "Extraction", "conflict": "Conflict", "deviation": "Deviation", "premise": "Premise",
         "(supersede)": "Supersession (extra)"}


def measured() -> dict[str, tuple[str, ...]]:
    """Every row of the measured run: hit, fp, miss, precision, recall."""
    rows = {}
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts and parts[0] in NAMES:
            rows[NAMES[parts[0]]] = tuple(parts[1:6])
    return rows


def test_counts_in_the_write_up_match_the_measured_run():
    table = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] in NAMES.values():
            table[cells[0]] = tuple(cells[1:6])
    assert table == measured() and len(table) == 5  # counts, precision and recall, supersession row included
    verbatim = re.search(r"verbatim quotes: (\d+)/(\d+)", RESULTS.read_text(encoding="utf-8"))
    assert f"{verbatim.group(1)} of {verbatim.group(2)}" in DOC.read_text(encoding="utf-8")


def test_failure_case_quotes_are_the_recorded_model_output():
    recorded = json.dumps(json.loads(RECORDED.read_text(encoding="utf-8")), ensure_ascii=False)
    quotes = re.findall(r"^> (.+)$", DOC.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert len(quotes) >= 2
    for quote in quotes:
        assert json.dumps(quote, ensure_ascii=False)[1:-1] in recorded, quote


def test_disclaimer_in_readme_and_write_up_and_both_are_english():
    for path in (ROOT / "README.md", DOC):
        assert DISCLAIMER in path.read_text(encoding="utf-8"), path
        assert check_no_cjk.scan(path) == [], path
