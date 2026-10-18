"""Held-out scenario (DL-010): shape, language and freeze rules; the pipeline never runs on it here."""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

from decision_ledger.bootstrap import DEMO_DIR
from decision_ledger.domain.models import SourceKind
from decision_ledger.services.parsers import parse_source

ROOT = Path(__file__).resolve().parent.parent
HELDOUT = ROOT / "demo" / "heldout"

_spec = importlib.util.spec_from_file_location("check_no_cjk", ROOT / "scripts" / "check_no_cjk.py")
check_no_cjk = importlib.util.module_from_spec(_spec)
sys.modules["check_no_cjk"] = check_no_cjk
_spec.loader.exec_module(check_no_cjk)


def manifest() -> dict:
    return json.loads((HELDOUT / "manifest.json").read_text(encoding="utf-8"))


def speakers(directory: Path, steps: list[dict]) -> set[str]:
    names: set[str] = set()
    for step in steps:
        if step["type"] == "prs":
            continue
        names |= {s.speaker for s in parse_source(SourceKind(step["kind"]), (directory / step["file"]).read_text(encoding="utf-8"))}
    return names


def test_shape_two_meetings_two_threads_three_prs():
    m = manifest()
    assert m["synthetic"] is True and m["held_out"] is True
    kinds = [s.get("kind") for s in m["steps"] if s["type"] == "source"]
    assert sorted(kinds) == ["slack", "slack", "transcript", "transcript"]
    [prs] = [s for s in m["steps"] if s["type"] == "prs"]
    assert len(prs["pull_requests"]) == 3
    for step in m["steps"]:
        files = [p["file"] for p in step.get("pull_requests", [])] or [step["file"]]
        assert all((HELDOUT / f).is_file() for f in files)
    assert all(parse_source(SourceKind(s["kind"]), (HELDOUT / s["file"]).read_text(encoding="utf-8"))
               for s in m["steps"] if s["type"] == "source")


def test_english_and_outside_the_runtime_package():
    assert HELDOUT.is_dir() and check_no_cjk.scan(HELDOUT) == []
    assert (ROOT / "src") not in HELDOUT.resolve().parents


def test_people_do_not_overlap_with_the_demo_team():
    demo_steps = json.loads((DEMO_DIR / "manifest.json").read_text(encoding="utf-8"))["steps"]
    assert speakers(HELDOUT, manifest()["steps"]).isdisjoint(speakers(DEMO_DIR, demo_steps))


def test_nothing_runs_the_pipeline_on_the_held_out_set():
    """Freeze rule: only this structural test may name the held-out set in tests, scripts or src; nothing runs it.

    The match is on the bare word "heldout" (any spelling of the path, e.g. Path("demo") / "heldout").
    """
    tracked = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "tests", "scripts", "src"], capture_output=True, text=True, cwd=ROOT, check=True).stdout.split()
    allowed = {"tests/test_heldout_set.py"}
    offenders = [f for f in tracked if f not in allowed and re.search(r"held[-_ ]?out", (ROOT / f).read_text(encoding="utf-8", errors="ignore"), re.IGNORECASE)]
    assert offenders == []


def test_steps_and_messages_are_in_chronological_order():
    def month_day(text: str) -> tuple[int, int]:
        month, day = text.split("-")[0].split("/")
        return int(month), int(day)

    steps = manifest()["steps"]
    dates = [month_day(s.get("occurred_on") or s["title"].split()[0]) for s in steps]
    assert dates == sorted(dates)
    names = [s["file"] for s in steps if s["type"] != "prs"] + [p["file"] for s in steps for p in s.get("pull_requests", [])]
    assert all(name[:2].isdigit() for name in names)
    for step in steps:
        if step.get("kind") == "slack":
            ts = [float(m["ts"]) for m in json.loads((HELDOUT / step["file"]).read_text(encoding="utf-8"))]
            assert ts == sorted(ts)


# --- gold answers (DL-013): structure and grounding only; the pipeline never runs on the held-out set ---

GOLD = ROOT / "demo" / "eval" / "heldout-gold.json"
HASHES = ROOT / "demo" / "eval" / "heldout-gold.sha256"


def gold() -> dict:
    return json.loads(GOLD.read_text(encoding="utf-8"))


def source_text(name: str) -> str:
    return (HELDOUT / name).read_text(encoding="utf-8").lower()


def test_gold_is_kept_apart_from_inputs_and_runtime():
    assert GOLD.is_file() and GOLD.parent != HELDOUT
    assert (ROOT / "src") not in GOLD.resolve().parents and HELDOUT not in GOLD.resolve().parents
    g = gold()
    assert g["synthetic"] is True and g["held_out"] is True and "Claire has not read" in g["labelled_by"]


def test_gold_decisions_are_grounded_in_the_inputs():
    sources = {s["file"] for s in manifest()["steps"] if s["type"] != "prs"}
    for item in gold()["decisions"]:
        assert item["source"] in sources, item
        # Same whole-word rule scripts/eval.py applies to decision statements.
        assert all(re.search(rf"(?<![\w/]){re.escape(k)}(?![\w])", source_text(item["source"])) for k in item["keywords"]), item
    for item in gold()["premise_negatives"]:
        assert item["quote"].lower() in source_text(item["source"]), item


def test_gold_references_resolve():
    g = gold()
    ids = {d["id"] for d in g["decisions"]}
    prs = {p["pr_number"] for s in manifest()["steps"] for p in s.get("pull_requests", [])}
    assert all(set(pair) <= ids for pair in g["conflicts"])
    assert all({s["old"], s["new"]} <= ids for s in g["supersessions"])
    assert all(d["decision"] in ids and d["pr"] in prs for d in g["deviations"])
    assert set(g["compliant_prs"]) <= prs and not set(g["compliant_prs"]) & {d["pr"] for d in g["deviations"]}
    assert all(p["decision"] in ids for p in g["premise_changes"])


def test_gold_and_inputs_match_the_recorded_hashes():
    """Frozen: any edit to the gold answers or the held-out inputs turns this red."""
    import hashlib

    recorded = dict(line.split("  ", 1)[::-1] for line in HASHES.read_text(encoding="utf-8").splitlines() if line.strip())
    files = [GOLD] + sorted(p for p in HELDOUT.iterdir() if p.is_file() and p.name != "FREEZE.md")
    actual = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    assert actual == recorded
