"""Score Decision Ledger against gold answers.

Runs a scenario (default: the demo manifest) through the configured LLM backend on
a throwaway SQLite database, then compares what the system recorded with a gold
file and prints hit / false positive / miss integers for four tasks: decision
extraction, conflicts, PR deviations and premise changes, plus the verbatim rate
of the quotes the model produced.

With the default Fake backend this replays the responses recorded from Gemini
(see ``src/decision_ledger/demo/fixtures/llm/RECORDING.md``), so the numbers
describe that recording, not a live model run.

Usage: ``uv run python scripts/eval.py [--gold demo/eval/dev-gold.json] [--out FILE]``
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from pydantic import BaseModel

from decision_ledger.adapters.github import DryRunCommentPublisher, GitHubPullRequestSource
from decision_ledger.adapters.llm.fake import FakeReplayLLM
from decision_ledger.adapters.storage.sqlite import SqliteLedgerStore
from decision_ledger.bootstrap import DEMO_DIR, ContainerFactory, Settings, build_llm, ensure_default_project, run_demo
from decision_ledger.domain.models import AlertKind, Decision
from decision_ledger.domain.ports import LLMClient, Repository
from decision_ledger.services.drafts import AssumptionCheck, ExtractionResult, QuoteRef, verify_quotes

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLD = ROOT / "demo" / "eval" / "dev-gold.json"
COUNTER_PATH = DEMO_DIR / "llm" / "record-calls.txt"
BUDGET = 60  # shared with scripts/record_demo.py
TASKS = ("extraction", "conflict", "deviation", "premise")


class EvalBudgetExceeded(RuntimeError):
    """Raised before calling a real backend once ``max_calls`` calls were made."""


class CapturingLLM:
    """Forwards to ``inner`` and keeps every structured response by key.

    For a Fake backend it also counts keys that have no recorded response
    (``replay_misses``), so a changed scenario cannot silently score as all misses.
    """

    def __init__(self, inner: LLMClient, *, max_calls: int | None = None):
        self.inner = inner
        self.name = inner.name
        self.max_calls = max_calls
        self.calls = 0
        self.replay_misses = 0
        self.responses: dict[str, BaseModel] = {}

    def generate_json(self, prompt, response_model, *, key):
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise EvalBudgetExceeded(f"evaluation budget of {self.max_calls} LLM calls is used up")
        self.calls += 1
        if isinstance(self.inner, FakeReplayLLM) and key not in self.inner.responses:
            self.replay_misses += 1
        result = self.inner.generate_json(prompt, response_model, key=key)
        self.responses[key] = result
        return result


@dataclass
class Count:
    hit: int = 0
    fp: int = 0
    miss: int = 0

    @property
    def precision(self) -> str:
        return _ratio(self.hit, self.hit + self.fp)

    @property
    def recall(self) -> str:
        return _ratio(self.hit, self.hit + self.miss)


@dataclass
class Metrics:
    counts: dict[str, Count]
    quotes_verbatim: int
    quotes_total: int
    supersession: Count = field(default_factory=Count)
    notes: list[str] = field(default_factory=list)

    @property
    def quote_rate(self) -> str:
        return _ratio(self.quotes_verbatim, self.quotes_total)


def _ratio(numerator: int, denominator: int) -> str:
    return "N/A" if denominator == 0 else f"{numerator / denominator:.2f}"


@dataclass
class Run:
    repo: Repository
    manifest: dict
    llm: CapturingLLM


def run_scenario(llm: LLMClient, demo_dir: Path = DEMO_DIR, *, max_calls: int | None = None) -> Run:
    """Run every manifest step on a fresh in-memory database; PR comments are always dry runs.

    Pass a :class:`CapturingLLM` to keep its call count when the run fails part-way.
    """
    capturing = llm if isinstance(llm, CapturingLLM) else CapturingLLM(llm, max_calls=max_calls)
    store = SqliteLedgerStore(":memory:")
    project = ensure_default_project(store, "Evaluation")
    factory = ContainerFactory(store, capturing, DryRunCommentPublisher(), GitHubPullRequestSource(None))
    container = factory.for_project(project.id)
    run_demo(container, demo_dir)
    manifest = json.loads((demo_dir / "manifest.json").read_text(encoding="utf-8"))
    return Run(repo=store.for_project(project.id), manifest=manifest, llm=capturing)


def _source_files(repo: Repository, manifest: dict) -> dict[str, str]:
    """Map source id to the manifest file name it was imported from (titles are unique per manifest)."""
    by_title = {step["title"]: step["file"] for step in manifest["steps"] if step["type"] != "prs"}
    return {s.id: by_title.get(s.title, s.title) for s in repo.list_sources()}


def has_keywords(text: str, keywords: Iterable[str]) -> bool:
    """Every keyword occurs as whole words (case-insensitive), so "november 3" does not match "november 30"."""
    return all(re.search(rf"(?<![\w/]){re.escape(k.lower())}(?![\w])", text.lower()) for k in keywords)


def _id_key(value: str) -> tuple[int, str]:
    return (int(value), "") if value.isdigit() else (sys.maxsize, value)


def match_decisions(decisions: Iterable[Decision], gold: list[dict], files: dict[str, str]) -> tuple[dict[str, str], Count]:
    """One-to-one greedy match of system decisions to gold decisions; returns system id -> gold id and the count."""
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    fp = 0
    for decision in sorted(decisions, key=lambda d: _id_key(d.id)):
        source = files.get(decision.evidence[0].source_id)
        gid = next(
            (g["id"] for g in gold if g["id"] not in taken and g["source"] == source and has_keywords(decision.statement, g["keywords"])),
            None,
        )
        if gid is None:
            fp += 1
        else:
            mapping[decision.id] = gid
            taken.add(gid)
    return mapping, Count(hit=len(taken), fp=fp, miss=len(gold) - len(taken))


def _pairs(found: list[tuple[str, ...] | None], gold: Iterable[tuple[str, ...]]) -> Count:
    """Each gold item can be hit once; unmapped, wrong and repeated predictions are false positives."""
    wanted, hits, fp = set(gold), set(), 0
    for item in found:
        if item is not None and item in wanted and item not in hits:
            hits.add(item)
        else:
            fp += 1
    return Count(hit=len(hits), fp=fp, miss=len(wanted - hits))


def score(run: Run, gold: dict) -> Metrics:
    """Compare what the system recorded with the gold answers."""
    repo, files = run.repo, _source_files(run.repo, run.manifest)
    mapping, extraction = match_decisions(repo.list_decisions(), gold["decisions"], files)
    alerts = repo.list_alerts()

    def gold_of(ids: Iterable[str]) -> tuple[str, ...] | None:
        mapped = [mapping.get(i) for i in ids]
        return None if None in mapped else tuple(sorted(mapped))

    conflicts = _pairs([gold_of(a.decision_ids) for a in alerts if a.kind is AlertKind.CONFLICT],
                       [tuple(sorted(p)) for p in gold.get("conflicts", [])])
    def ordered(old: str | None, new: str | None) -> tuple[str, ...] | None:
        pair = (mapping.get(old or ""), mapping.get(new or ""))
        return None if None in pair else pair

    supersession = _pairs([ordered(a.superseded_id, a.superseding_id) for a in alerts if a.kind is AlertKind.SUPERSESSION],
                          [(s["old"], s["new"]) for s in gold.get("supersessions", [])])
    checks = {c.key: c for c in repo.list_pr_checks()}
    deviations = _pairs(
        [(str(checks[a.pr_check_key].pr_number), *(gold_of(a.decision_ids) or ("?",))) if a.pr_check_key in checks else None
         for a in alerts if a.kind is AlertKind.DEVIATION],
        [(str(d["pr"]), d["decision"]) for d in gold.get("deviations", [])],
    )
    negatives = [n["quote"].lower() for n in gold.get("premise_negatives", [])]
    premise_found = []
    for a in (a for a in alerts if a.kind is AlertKind.ASSUMPTION):
        gid = mapping.get(a.decision_ids[0])
        hit = next(
            (p for p in gold.get("premise_changes", [])
             if p["decision"] == gid
             and has_keywords(a.assumption_text or "", p["premise_keywords"])
             and any(has_keywords(e.quote, p["evidence_keywords"]) and not any(n in e.quote.lower() or e.quote.lower() in n for n in negatives)
                     for e in a.evidence if files.get(e.source_id) == p["source"])),
            None,
        )
        premise_found.append((hit["decision"], hit["source"]) if hit else None)
    premises = _pairs(premise_found, [(p["decision"], p["source"]) for p in gold.get("premise_changes", [])])
    verbatim, total = quote_rate(run)
    notes = [f"replay misses: {run.llm.replay_misses}"] if isinstance(run.llm.inner, FakeReplayLLM) else []
    return Metrics(
        counts={"extraction": extraction, "conflict": conflicts, "deviation": deviations, "premise": premises},
        quotes_verbatim=verbatim, quotes_total=total, supersession=supersession, notes=notes,
    )


def quote_rate(run: Run) -> tuple[int, int]:
    """Quotes in the model's raw extraction and premise responses that occur in the source (same check as ingestion)."""
    verbatim = total = 0
    for key, response in run.llm.responses.items():
        kind, _, digest = key.partition(":")
        refs: list[QuoteRef] = []
        if kind == "extract" and isinstance(response, ExtractionResult):
            for d in response.decisions:
                refs += d.evidence + [q for a in d.assumptions for q in a.evidence]
        elif kind == "assumption" and isinstance(response, AssumptionCheck):
            refs += [q for f in response.findings for q in f.evidence]
        else:
            continue
        source = run.repo.find_source_by_hash(digest)
        segments = source.segments if source else []
        for ref in refs:
            total += 1
            verbatim += len(verify_quotes([ref], segments))
    return verbatim, total


def render(metrics: Metrics, header: Iterable[str] = ()) -> str:
    """Plain-text report with integers first; ratios show N/A when the denominator is zero."""
    lines = list(header)
    lines.append(f"{'task':<12} {'hit':>4} {'fp':>4} {'miss':>4}  {'precision':>9} {'recall':>6}")
    for task in TASKS:
        c = metrics.counts[task]
        lines.append(f"{task:<12} {c.hit:>4} {c.fp:>4} {c.miss:>4}  {c.precision:>9} {c.recall:>6}")
    s = metrics.supersession
    lines.append(f"{'(supersede)':<12} {s.hit:>4} {s.fp:>4} {s.miss:>4}  {s.precision:>9} {s.recall:>6}")
    lines.append(f"verbatim quotes: {metrics.quotes_verbatim}/{metrics.quotes_total} ({metrics.quote_rate})")
    lines += metrics.notes
    return "\n".join(lines) + "\n"


def _head() -> str:
    """Short HEAD, with "-dirty" when the working tree has uncommitted changes."""
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=ROOT, check=True).stdout.strip()
        return f"{head}-dirty" if dirty else head
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def used_calls(counter: Path) -> int:
    return int(counter.read_text(encoding="utf-8").strip() or 0) if counter.is_file() else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Decision Ledger against gold answers.")
    parser.add_argument("--gold", default=str(DEFAULT_GOLD))
    parser.add_argument("--scenario", default=str(DEMO_DIR), help="directory holding manifest.json and the sources")
    parser.add_argument("--max-calls", type=int, default=40, help="hard stop for real LLM backends")
    parser.add_argument("--counter", default=str(COUNTER_PATH), help="LLM call ledger shared with scripts/record_demo.py")
    parser.add_argument("--out", help="also write the report to this file (only for a complete run)")
    args = parser.parse_args(argv)
    # Never record while evaluating: a recorder pointed at demo.json would replace the kept fixtures.
    settings = replace(Settings.from_env(), llm_record_path=None)
    real = settings.llm_backend != "fake"
    counter = Path(args.counter)
    max_calls = min(args.max_calls, max(BUDGET - used_calls(counter), 0)) if real else None
    capturing = CapturingLLM(build_llm(settings), max_calls=max_calls)
    try:
        run = run_scenario(capturing, Path(args.scenario))
    except EvalBudgetExceeded as exc:
        print(f"incomplete: {exc}; no results written")
        return 3
    finally:
        # Every call that reached a real backend is charged, whatever stopped the run.
        if real:
            counter.write_text(f"{used_calls(counter) + capturing.calls}\n", encoding="utf-8")
    gold_path = Path(args.gold)
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    header = [
        f"Decision Ledger evaluation, {dt.date.today().isoformat()}, HEAD {_head()}",
        f"scenario: {Path(args.scenario).name}  gold: {gold_path.name} (sha256 {_sha(gold_path)})  scorer: eval.py (sha256 {_sha(Path(__file__))})",
        f"backend: {settings.llm_backend}"
        + (" (replay of responses recorded from Gemini, see src/decision_ledger/demo/fixtures/llm/RECORDING.md)" if not real else f" ({settings.gemini_model}, live)"),
        "Synthetic fixtures. Gold labelled by the AI team on Ci's behalf (development set, see the gold file's labelled_by).",
        "Results describe this test set only; they do not establish production accuracy.",
        "",
    ]
    metrics = score(run, gold)
    report = render(metrics, header)
    print(report, end="")
    if run.llm.replay_misses:
        print(f"incomplete: {run.llm.replay_misses} LLM call(s) had no recorded response; no results written")
        return 4
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
