"""Detect PR changes that deviate from checkable decisions.

Results are advisory: a deviation produces an alert and a PR comment, never a
failing commit status. Each PR revision is checked once (idempotency key).
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from decision_ledger.domain.errors import DuplicatePrCheck, NotFound, ReplayNotReady
from decision_ledger.domain.models import (
    Alert,
    AlertKind,
    Decision,
    DecisionStatus,
    Evidence,
    PrCheck,
    PullRequestDiff,
    PullRequestEvent,
)
from decision_ledger.domain.ports import CommentPublisher, LLMClient, PullRequestSource, Repository
from decision_ledger.services.qa import terms
from decision_ledger.services.structured import call_structured
from decision_ledger.services.drafts import decision_ref

DEFAULT_DIFF_CHAR_LIMIT = 20_000

PROMPT = """You check a pull request against the team's architecture decisions.
Report only clear violations of a listed decision by ADDED lines (+). Do not report style issues.
For each violation give decision_id (without '#'), file, line (new line number), the offending snippet, and a short explanation.

DECISIONS:
{decisions}

{diff_header}
{diff}
"""

RELEVANT_DIFF_HEADER = 'DIFF (only files relevant to the decisions; lines are "<new line no> <+|-| > <code>"):'
WHOLE_DIFF_HEADER = (
    "DIFF (the whole diff: at least one decision's path hints matched no file; path hints are guesses and may not "
    'match the real paths, so judge by what the code does; lines are "<new line no> <+|-| > <code>"):'
)

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")

DiffLine = tuple[int | None, str, str]


class Violation(BaseModel):
    decision_id: str
    file: str
    line: int | None = None
    snippet: str = ""
    explanation: str = ""


class DeviationJudgement(BaseModel):
    violations: list[Violation] = Field(default_factory=list)


@dataclass
class DeviationResult:
    check: PrCheck
    alerts: list[Alert] = field(default_factory=list)
    reused: bool = False


def idempotency_key(pr: PullRequestDiff) -> str:
    """Identify a PR revision: ``repo#number@sha`` when known, else the diff's sha256."""
    if pr.repo and pr.pr_number is not None and pr.head_sha:
        return f"{pr.repo}#{pr.pr_number}@{pr.head_sha}"
    return "sha256:" + hashlib.sha256(pr.diff.encode("utf-8")).hexdigest()


def parse_diff(diff: str) -> dict[str, list[DiffLine]]:
    """Parse a unified diff into ``{path: [(new_line_no | None, kind, text)]}``."""
    files: dict[str, list[DiffLine]] = {}
    current: list[DiffLine] | None = None
    new_line = 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            current = None
        elif raw.startswith("+++ "):
            path = raw[4:].strip()
            path = path[2:] if path.startswith("b/") else path
            current = files.setdefault(path, [])
        elif raw.startswith("--- ") or current is None:
            continue
        elif match := _HUNK.match(raw):
            new_line = int(match["start"])
        elif raw.startswith("+"):
            current.append((new_line, "+", raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            current.append((None, "-", raw[1:]))
        elif not raw.startswith("\\"):
            current.append((new_line, " ", raw[1:] if raw.startswith(" ") else raw))
            new_line += 1
    return files


def matches(path: str, patterns: Sequence[str]) -> bool:
    """True when ``path`` matches a glob pattern, or lies under a plain directory pattern."""
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatch(path, pattern):
                return True
        elif path == pattern or path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


def best_evidence(decision: Decision) -> Evidence:
    """The evidence whose wording overlaps the decision statement most; ties keep the earliest."""
    wanted = set(terms(decision.statement))
    best, best_score = decision.evidence[0], -1
    for item in decision.evidence:
        score = len(wanted & set(terms(item.quote)))
        if score > best_score:
            best, best_score = item, score
    return best


def _where(violation: Violation) -> str:
    return f"{violation.file}:{violation.line}" if violation.line else violation.file


def group_by_decision(found: Sequence[tuple[Violation, Decision]]) -> list[tuple[Decision, list[Violation]]]:
    """Group violations by decision, keeping the order in which decisions first appear."""
    groups: dict[str, tuple[Decision, list[Violation]]] = {}
    for violation, decision in found:
        _, violations = groups.setdefault(decision.id, (decision, []))
        if all(_where(v) != _where(violation) for v in violations):
            violations.append(violation)
    return list(groups.values())


def render_comment(
    groups: Sequence[tuple[Decision, Sequence[Violation]]],
    compared: Sequence[Decision],
    *,
    truncated: bool,
    whole_diff: bool = False,
    link: Callable[[Decision], str] | None = None,
) -> str:
    """Render the advisory PR comment (Markdown); one section per violated decision."""
    lines: list[str] = []
    if groups:
        n = len(groups)
        lines.append(f"**Decision Ledger: this PR may violate {n} architecture decision{'' if n == 1 else 's'}**")
        for decision, violations in groups:
            lines += ["", f"### Decision #{decision.id}: {decision.statement}"]
            lines.append(f"- Decided by: {decision.decided_by}" + (f" ({decision.decided_on})" if decision.decided_on else ""))
            lines.append(f"- Rationale: {decision.rationale}")
            for violation in violations:
                lines.append(f"- Possible violation: `{_where(violation)}`")
                if violation.snippet:
                    lines.append(f"  ```\n  {violation.snippet}\n  ```")
                if violation.explanation:
                    lines.append(f"- Why: {violation.explanation}")
            quote = best_evidence(decision)
            lines.append(f"- Original quote: \"{quote.quote}\" ({quote.locator}, {quote.speaker})")
            if link is not None:
                lines.append(f"- Open in Decision Ledger: {link(decision)}")
    elif compared:
        ids = ", ".join(f"#{d.id}" for d in compared)
        n = len(compared)
        scope = "compared the whole diff with" if whole_diff else "compared"
        noun = "checkable architecture decision" if whole_diff else "related architecture decision"
        lines.append(f"✓ Decision Ledger: {scope} {n} {noun}{'' if n == 1 else 's'} ({ids}); no violation found.")
    else:
        lines.append("✓ Decision Ledger: no checkable decision relates to this PR; no violation found.")
    if truncated:
        lines += ["", "> The diff was too large; some hunks were truncated before the check."]
    lines += ["", "_Advisory only; this comment never blocks a merge._"]
    return "\n".join(lines)


class DeviationService:
    def __init__(
        self,
        repo: Repository,
        llm: LLMClient,
        publisher: CommentPublisher,
        pr_source: PullRequestSource,
        *,
        diff_char_limit: int = DEFAULT_DIFF_CHAR_LIMIT,
        public_url: str | None = None,
        replays: Mapping[int, PullRequestDiff] | None = None,
        replay_requires: Sequence[str] = (),
    ):
        self.repo = repo
        self.public_url = public_url
        self.replays = dict(replays or {})
        self.replay_requires = tuple(replay_requires)
        self.llm = llm
        self.publisher = publisher
        self.pr_source = pr_source
        self.diff_char_limit = diff_char_limit

    def resolve_webhook(self, event: PullRequestEvent) -> PullRequestDiff:
        """Get the diff of a webhook event (payload or read-only API); raises ``DiffUnavailable``."""
        return self.pr_source.fetch_diff(event)

    def replay(self, pr_number: int) -> DeviationResult:
        """Re-run the check on a built-in demo PR; no GitHub call.

        Refuses (``ReplayNotReady``) until every demo source before the PR step is imported and some
        decision is checkable: the result is cached by revision key, so an early replay would stick.
        """
        pr = self.replays.get(pr_number)
        if pr is None:
            raise NotFound(f"no built-in PR #{pr_number} to replay")
        missing = [h for h in self.replay_requires if self.repo.find_source_by_hash(h) is None]
        if missing or not self.repo.list_checkable_decisions():
            raise ReplayNotReady("import the demo sources before replaying a PR")
        return self.check(pr)

    def check_webhook(self, event: PullRequestEvent) -> DeviationResult:
        """Resolve the diff of a webhook event, then check it."""
        return self.check(self.resolve_webhook(event))

    def check(self, pr: PullRequestDiff) -> DeviationResult:
        """Check one PR revision; a repeated revision returns the stored result without side effects."""
        key = idempotency_key(pr)
        existing = self.repo.get_pr_check(key)
        if existing is not None:
            return self._reused(existing)

        decisions = self.repo.list_checkable_decisions()
        relevant_text, relevant, truncated, whole_diff = self._relevant_hunks(parse_diff(pr.diff), decisions)
        found: list[tuple[Violation, Decision]] = []
        if relevant_text:
            judgement = call_structured(
                self.llm,
                PROMPT.format(
                    decisions="\n".join(
                        f"- #{d.id} {d.statement} — rule: {d.checkable_rule.description} (paths: {', '.join(d.checkable_rule.path_patterns)})"
                        for d in relevant
                    ),
                    diff_header=WHOLE_DIFF_HEADER if whole_diff else RELEVANT_DIFF_HEADER,
                    diff=relevant_text,
                ),
                DeviationJudgement,
                key=f"deviation:{key}",
            )
            by_id = {d.id: d for d in relevant}
            found = [(v, by_id[decision_ref(v.decision_id)]) for v in judgement.violations if decision_ref(v.decision_id) in by_id]
        try:
            with self.repo.unit_of_work() as writer:
                # A decision may have been superseded during the LLM call: re-validate inside the transaction.
                found = [
                    (v, current)
                    for v, decision in found
                    if (current := writer.get_decision(decision.id)) is not None and current.status is DecisionStatus.ACTIVE
                ]
                groups = group_by_decision(found)
                link = self._decision_link if self.public_url else None
                comment = render_comment(groups, relevant, truncated=truncated, whole_diff=whole_diff, link=link)
                alerts = [
                    Alert(
                        id=writer.allocate_id("alert"),
                        kind=AlertKind.DEVIATION,
                        decision_ids=[decision.id],
                        location=_where(violations[0]),
                        pr_check_key=key,
                        summary=_alert_summary(decision, violations),
                        evidence=_alert_evidence(decision),
                    )
                    for decision, violations in groups
                ]
                for alert in alerts:
                    writer.save_alert(alert)
                check = PrCheck(
                    key=key, repo=pr.repo, pr_number=pr.pr_number, head_sha=pr.head_sha, title=pr.title,
                    author=pr.author, diff=pr.diff, comment=comment, violated=bool(found), truncated=truncated,
                    alert_ids=[a.id for a in alerts],
                )
                writer.save_pr_check(check)
        except DuplicatePrCheck:
            return self._reused(self.repo.get_pr_check(key))

        published = self.publisher.publish(pr, comment)
        if published is not None:
            check = check.model_copy(update={"comment_published": published})
            with self.repo.unit_of_work() as writer:
                writer.save_pr_check(check)
        return DeviationResult(check, alerts)

    def _decision_link(self, decision: Decision) -> str:
        return f"{self.public_url}/#p={self.repo.project_id}&d={decision.id}"

    def _reused(self, check: PrCheck | None) -> DeviationResult:
        if check is None:
            raise NotFound("PR check disappeared after a duplicate write")
        alerts = [a for a in (self.repo.get_alert(i) for i in check.alert_ids) if a is not None]
        return DeviationResult(check, alerts, reused=True)

    def _relevant_hunks(
        self, files: dict[str, list[DiffLine]], decisions: Sequence[Decision]
    ) -> tuple[str, list[Decision], bool, bool]:
        """Pick the diff text and decisions to send: ``(text, decisions, truncated, whole_diff)``.

        Path patterns come from the LLM and may be guesses. When some checkable decision's patterns
        match no file in the diff, the whole diff is sent with every checkable decision, provided it
        fits within the limit. Otherwise only the matched files are sent (truncated at the limit).
        """
        checkable = [d for d in decisions if d.checkable_rule]
        relevant: dict[str, Decision] = {}
        matched: dict[str, list[DiffLine]] = {}
        for path, lines in files.items():
            hit = [d for d in checkable if matches(path, d.checkable_rule.path_patterns)]
            if hit:
                relevant.update({d.id: d for d in hit})
                matched[path] = lines
        unanchored = len(relevant) < len(checkable)
        if unanchored:
            whole = _format_files(files)
            if whole and len(whole) <= self.diff_char_limit:
                return whole, checkable, False, True
        if matched:
            text = _format_files(matched)
            truncated = len(text) > self.diff_char_limit
            return (text[: self.diff_char_limit] if truncated else text), list(relevant.values()), truncated, False
        return "", [], False, False


def _format_files(files: dict[str, list[DiffLine]]) -> str:
    chunks = []
    for path, lines in files.items():
        body = "\n".join(f"{n if n is not None else '':>5} {k} {t}" for n, k, t in lines)
        chunks.append(f"### {path}\n{body}")
    return "\n\n".join(chunks)


def _alert_summary(decision: Decision, violations: Sequence[Violation]) -> str:
    first = violations[0]
    summary = first.explanation or f"PR may violate decision #{decision.id}"
    others = [_where(v) for v in violations[1:]]
    return summary + (f" (see also {', '.join(others)})" if others else "")


def _alert_evidence(decision: Decision) -> list[Evidence]:
    best = best_evidence(decision)
    return [best, *(e for e in decision.evidence if e is not best)][:2]
