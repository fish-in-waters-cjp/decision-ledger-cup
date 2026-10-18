"""Import a source: parse → extract → verify evidence → reconcile → watch assumptions → write once."""

from __future__ import annotations

from decision_ledger.domain.errors import DuplicateSource
from decision_ledger.domain.models import (
    Alert,
    AlertKind,
    Assumption,
    CheckableRule,
    Decision,
    DecisionEvent,
    DecisionStatus,
    DiscardedItem,
    ImportReport,
    Segment,
    Source,
    SourceKind,
    Topic,
)
from decision_ledger.domain.ports import LedgerWriter, LLMClient, Repository
from decision_ledger.services.assumption_watch import AssumptionAlertDraft, AssumptionWatchService
from decision_ledger.services.drafts import (
    DecisionDraft,
    DraftAssumption,
    ExtractedDecision,
    ExtractionResult,
    Relation,
    render_segments,
    verify_quotes,
)
from decision_ledger.services.parsers import content_hash, parse_source
from decision_ledger.services.reconciliation import ReconcileOutcome, ReconciliationService
from decision_ledger.services.structured import call_structured
from decision_ledger.services.topics import TopicAssigner, TopicChoice

PROMPT = """Extract every decision the team made in the conversation below.
A decision is a conclusion someone with authority agreed on (not a suggestion or open question).

For each decision return:
- topic: a short subject. Reuse one of the EXISTING TOPICS verbatim when it is the same subject.
- statement: the decision in one sentence.
- rationale: why it was decided.
- decided_by: who decided.
- assumptions: facts the decision relies on that could later turn out false, each with evidence quotes.
- checkable_rule: only for rules that can be checked in code changes — description and path_patterns (glob patterns of affected files); otherwise null.
- evidence: quotes copied verbatim from the segments, each with its segment_index.

Return an empty list when there is no decision. Keep the original language of the conversation.

EXISTING TOPICS: {topics}

SEGMENTS:
{segments}
"""

_LIVE = (DecisionStatus.ACTIVE, DecisionStatus.UNDER_REVIEW)


class IngestionService:
    def __init__(
        self,
        repo: Repository,
        llm: LLMClient,
        reconciliation: ReconciliationService,
        assumption_watch: AssumptionWatchService,
        topic_assigner: TopicAssigner | None = None,
    ):
        self.repo = repo
        self.llm = llm
        self.reconciliation = reconciliation
        self.assumption_watch = assumption_watch
        self.topic_assigner = topic_assigner or TopicAssigner(llm)

    def ingest(self, kind: SourceKind, title: str, raw_text: str, occurred_on: str | None = None) -> ImportReport:
        """Import one source and return what happened. Writes everything in a single transaction."""
        segments = parse_source(kind, raw_text)
        digest = content_hash(raw_text)
        existing_source = self.repo.find_source_by_hash(digest)
        if existing_source is not None:
            return ImportReport(source_id=existing_source.id, duplicate=True, segment_count=len(existing_source.segments))

        existing = self.repo.list_decisions(statuses=_LIVE)
        topics = self.repo.list_topics(include_merged=True)
        live_names = sorted(t.name for t in topics if t.merged_into is None)
        report = ImportReport(segment_count=len(segments))

        extraction = call_structured(
            self.llm,
            PROMPT.format(topics=", ".join(live_names) or "(none)", segments=render_segments(segments)),
            ExtractionResult,
            key=f"extract:{digest}",
        )
        drafts = self._verified_drafts(extraction.decisions, segments, report)
        choices = self.topic_assigner.assign(drafts, topics, content_hash=digest)
        topic_of = {d.index: c.topic_id for d, c in zip(drafts, choices, strict=True)}
        outcomes = self.reconciliation.reconcile(drafts, existing, content_hash=digest, topic_of=topic_of)
        assumption_alerts, discarded = self.assumption_watch.check(segments, existing, content_hash=digest)
        report.discarded.extend(discarded)

        try:
            with self.repo.unit_of_work() as writer:
                source_id = writer.allocate_id("source")
                writer.save_source(
                    Source(id=source_id, kind=kind, title=title, occurred_on=occurred_on, content_hash=digest, segments=segments)
                )
                report.source_id = source_id
                for outcome, choice in zip(outcomes, choices, strict=True):
                    self._write_outcome(writer, outcome, choice, source_id, occurred_on, report)
                for draft in assumption_alerts:
                    self._write_assumption_alert(writer, draft, source_id, report)
        except DuplicateSource:
            winner = self.repo.find_source_by_hash(digest)
            return ImportReport(source_id=winner.id if winner else None, duplicate=True, segment_count=len(segments))
        return report

    @staticmethod
    def _verified_drafts(
        extracted: list[ExtractedDecision], segments: list[Segment], report: ImportReport
    ) -> list[DecisionDraft]:
        drafts: list[DecisionDraft] = []
        for item in extracted:
            evidence = verify_quotes(item.evidence, segments)
            if not evidence:
                reason = "No source quote" if not item.evidence else "The quote was not found in the source"
                report.discarded.append(DiscardedItem(kind="decision", summary=item.statement, reason=reason))
                continue
            drafts.append(
                DecisionDraft(
                    index=len(drafts),
                    topic=item.topic,
                    statement=item.statement,
                    rationale=item.rationale,
                    decided_by=item.decided_by or evidence[0].speaker,
                    assumptions=[
                        DraftAssumption(text=a.text, evidence=verify_quotes(a.evidence, segments)) for a in item.assumptions
                    ],
                    checkable_rule=(
                        CheckableRule(description=item.checkable_rule.description, path_patterns=item.checkable_rule.path_patterns)
                        if item.checkable_rule
                        else None
                    ),
                    evidence=evidence,
                )
            )
        return drafts

    @staticmethod
    def _topic_for(writer: LedgerWriter, choice: TopicChoice, report: ImportReport) -> Topic:
        """Re-resolve the chosen topic inside the transaction; create it once per name when it does not exist."""
        topic = writer.resolve_topic(choice.topic_id) if choice.topic_id else None
        topic = topic or writer.find_topic_by_name(choice.name)
        if topic is not None:
            report.topics_existing += 1
            return topic
        topic = Topic(id=writer.allocate_id("topic"), name=choice.name)
        writer.save_topic(topic)
        report.topics_created += 1
        return topic

    @classmethod
    def _write_outcome(
        cls,
        writer: LedgerWriter,
        outcome: ReconcileOutcome,
        choice: TopicChoice,
        source_id: str,
        occurred_on: str | None,
        report: ImportReport,
    ) -> None:
        draft = outcome.draft
        evidence = [q.to_evidence(source_id) for q in draft.evidence]
        # The ledger may have changed during the LLM calls: re-read the target inside the transaction.
        existing = writer.get_decision(outcome.existing.id) if outcome.existing is not None else None
        if existing is not None and existing.status not in _LIVE:
            existing = None
        if outcome.relation is Relation.MERGE and existing is not None:
            writer.add_evidence(existing.id, evidence)
            writer.append_event(DecisionEvent(decision_id=existing.id, kind="merged", note=outcome.explanation))
            report.merged.append(existing.id)
            report.topics_existing += 1
            return

        decision = Decision(
            id=writer.allocate_id("decision"),
            topic_id=cls._topic_for(writer, choice, report).id,
            topic=draft.topic,
            statement=draft.statement,
            rationale=draft.rationale,
            decided_by=draft.decided_by,
            decided_on=occurred_on,
            assumptions=[
                Assumption(text=a.text, evidence=[q.to_evidence(source_id) for q in a.evidence]) for a in draft.assumptions
            ],
            checkable_rule=draft.checkable_rule,
            evidence=evidence,
        )
        writer.save_decision(decision)
        writer.append_event(DecisionEvent(decision_id=decision.id, kind="created"))
        report.created.append(decision.id)

        if existing is None or outcome.relation in (Relation.NEW, Relation.MERGE):
            return
        both_sides = [*existing.evidence[:1], *evidence]
        if outcome.relation is Relation.SUPERSEDE:
            alert = Alert(
                id=writer.allocate_id("alert"),
                kind=AlertKind.SUPERSESSION,
                decision_ids=[existing.id, decision.id],
                superseded_id=existing.id,
                superseding_id=decision.id,
                summary=outcome.explanation or f"#{decision.id} may supersede #{existing.id}",
                evidence=both_sides,
            )
            report.supersession_alerts.append(alert.id)
        else:
            alert = Alert(
                id=writer.allocate_id("alert"),
                kind=AlertKind.CONFLICT,
                decision_ids=[existing.id, decision.id],
                summary=outcome.explanation or f"#{decision.id} conflicts with #{existing.id}",
                evidence=both_sides,
            )
            report.conflict_alerts.append(alert.id)
        writer.save_alert(alert)

    @staticmethod
    def _write_assumption_alert(
        writer: LedgerWriter, draft: AssumptionAlertDraft, source_id: str, report: ImportReport
    ) -> None:
        current = writer.get_decision(draft.decision.id)
        if current is None or current.status is not DecisionStatus.ACTIVE:
            report.discarded.append(
                DiscardedItem(kind="assumption_alert", summary=f"#{draft.decision.id} {draft.assumption.text}", reason="The decision status has changed")
            )
            return
        # Both sides are required: fall back to the decision's own evidence when the assumption's quote was unverifiable.
        original = draft.assumption.evidence or current.evidence[:1]
        alert = Alert(
            id=writer.allocate_id("alert"),
            kind=AlertKind.ASSUMPTION,
            decision_ids=[current.id],
            assumption_text=draft.assumption.text,
            summary=draft.explanation or f"A premise of decision #{current.id} may no longer hold",
            evidence=[*original, *(q.to_evidence(source_id) for q in draft.new_evidence)],
        )
        writer.save_alert(alert)
        report.assumption_alerts.append(alert.id)
