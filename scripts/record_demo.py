"""Record the demo scenario against a real LLM and save the responses as Fake fixtures.

Runs every manifest step and question through ``RecordingLLM`` on a fresh SQLite
database, then replaces ``demo/fixtures/llm/demo.json`` in one move so no stale
keys survive. Every call that reaches the real backend is added to a counter file;
the whole recording effort has a hard budget of ``BUDGET`` calls.

Usage (Vertex AI Gemini):

    DL_LLM_BACKEND=gemini DL_GEMINI_VERTEXAI=1 DL_GEMINI_MODEL=gemini-3.5-flash \\
    GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=global \\
    [DL_GEMINI_ACCESS_TOKEN="$(gcloud auth print-access-token)"] \\
    uv run --extra gemini python scripts/record_demo.py [--require-all] [--counter PATH]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from decision_ledger.adapters.github import DryRunCommentPublisher, GitHubPullRequestSource
from decision_ledger.adapters.llm.recording import RecordingLLM
from decision_ledger.adapters.storage.sqlite import SqliteLedgerStore
from decision_ledger.bootstrap import DEMO_DIR, ContainerFactory, Settings, build_llm, ensure_default_project, run_demo
from decision_ledger.domain.models import AlertKind, AlertStatus
from decision_ledger.domain.ports import LLMClient

BUDGET = 120  # raised from 60 on 2026-10-17 for the DL-009 re-record (commander decision)
OUT_PATH = DEMO_DIR / "llm" / "demo.json"
NOTES_PATH = DEMO_DIR / "llm" / "RECORDING.md"
# Tracked on purpose: the budget covers every recording on every machine, not one session.
COUNTER_PATH = DEMO_DIR / "llm" / "record-calls.txt"
MOMENTS = ("conflict", "assumption", "deviation")


class _ReuseRecorded:
    """Answer keys already in a partial recording without paying for them again (``--resume``)."""

    def __init__(self, recorder: RecordingLLM, done: dict[str, object]):
        self.recorder = recorder
        self.done = done
        self.name = recorder.name

    def generate_json(self, prompt, response_model, *, key):
        if key in self.done:
            return response_model.model_validate(self.done[key])
        return self.recorder.generate_json(prompt, response_model, key=key)


@dataclass
class RecordResult:
    calls: int
    summary: dict[str, int]
    moments: dict[str, bool] = field(default_factory=dict)
    pr_violated: dict[int, bool] = field(default_factory=dict)
    answers_found: int = 0
    replaced: bool = False


def used_calls(counter_path: Path) -> int:
    """Calls already spent by earlier recordings (0 when the counter file does not exist)."""
    return int(counter_path.read_text(encoding="utf-8").strip() or 0) if counter_path.is_file() else 0


def record(
    inner: LLMClient,
    *,
    out_path: Path = OUT_PATH,
    counter_path: Path,
    db_dir: Path | None = None,
    demo_dir: Path = DEMO_DIR,
    require_all: bool = False,
    resume: bool = False,
) -> RecordResult:
    """Record one full demo run; the output is replaced only when the run completes (and, with
    ``require_all``, only when every demo moment appeared)."""
    remaining = BUDGET - used_calls(counter_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Beside the output (same file system, so the final rename is atomic); the suffix keeps
    # FakeReplayLLM, which loads *.json, from reading a half-written recording. A failed run
    # leaves it in place so responses already paid for are not lost.
    staging = out_path.with_name(out_path.name + ".partial")
    if not resume:
        staging.unlink(missing_ok=True)
    done = json.loads(staging.read_text(encoding="utf-8")) if staging.is_file() else {}
    recorder = RecordingLLM(inner, staging, max_calls=max(remaining, 0))
    llm = _ReuseRecorded(recorder, done) if done else recorder
    with tempfile.TemporaryDirectory(dir=db_dir) as tmp:
        store = SqliteLedgerStore(str(Path(tmp) / "record.db"))
        try:
            project = ensure_default_project(store, "Recording")
            factory = ContainerFactory(store, llm, DryRunCommentPublisher(), GitHubPullRequestSource(None))
            container = factory.for_project(project.id)
            summary = run_demo(container, demo_dir)
            manifest = json.loads((demo_dir / "manifest.json").read_text(encoding="utf-8"))
            answers = [container.qa.answer(q) for q in manifest.get("questions", [])]
            overview = container.query.overview()
            open_kinds = Counter(a.kind for a in overview.open_alerts if a.status is AlertStatus.OPEN)
            result = RecordResult(
                calls=recorder.calls,
                summary=summary,
                moments={kind: open_kinds.get(AlertKind(kind), 0) > 0 for kind in MOMENTS},
                pr_violated={c.pr_number: c.violated for c in container.query.pr_checks()},
                answers_found=sum(1 for a in answers if a.found),
            )
        finally:
            store.close()
            counter_path.parent.mkdir(parents=True, exist_ok=True)
            counter_path.write_text(f"{used_calls(counter_path) + recorder.calls}\n", encoding="utf-8")
    if require_all and not all(result.moments.values()):
        result.replaced = False
        return result
    staging.replace(out_path)
    result.replaced = True
    return result


def write_notes(result: RecordResult, settings: Settings, total_used: int, path: Path = NOTES_PATH) -> None:
    """Record which model produced the fixtures, so the video and the submission can match them."""
    path.write_text(
        "# Recorded demo responses\n\n"
        "`demo.json` holds real LLM responses recorded by `scripts/record_demo.py` and replayed by the Fake backend.\n"
        "The scenario is synthetic: the Atlas team, TrailCart and every person are invented.\n\n"
        f"- Backend: {settings.llm_backend}\n"
        f"- Model: {settings.gemini_model}\n"
        f"- Location: {settings.gcp_location or 'default'}\n"
        f"- Recorded on: {dt.date.today().isoformat()}\n"
        f"- LLM calls in this recording: {result.calls} (recording budget used so far: {total_used} of {BUDGET})\n"
        f"- Summary: {json.dumps(result.summary, sort_keys=True)}\n"
        f"- Demo moments: {json.dumps(result.moments, sort_keys=True)}\n"
        f"- PR violated: {json.dumps({str(k): v for k, v in sorted(result.pr_violated.items())})}\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--counter", default=str(COUNTER_PATH), help="call counter file (tracked in the repo)")
    parser.add_argument("--resume", action="store_true", help="reuse responses already in demo.json.partial from a failed run")
    parser.add_argument("--require-all", action="store_true", help="keep the current fixtures unless every demo moment appears")
    args = parser.parse_args(argv)
    settings = replace(Settings.from_env(), llm_record_path=None)
    if settings.llm_backend == "fake":
        print("Set DL_LLM_BACKEND to a real backend (gemini) to record.", file=sys.stderr)
        return 2
    counter = Path(args.counter)
    print(f"Budget: {used_calls(counter)} of {BUDGET} calls already used.")
    result = record(build_llm(settings), counter_path=counter, require_all=args.require_all, resume=args.resume)
    total = used_calls(counter)
    if result.replaced:
        write_notes(result, settings, total)
    print(json.dumps({"calls": result.calls, "total_used": total, "summary": result.summary,
                      "moments": result.moments, "pr_violated": result.pr_violated,
                      "answers_found": result.answers_found, "replaced": result.replaced}, indent=2, sort_keys=True))
    return 0 if all(result.moments.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
