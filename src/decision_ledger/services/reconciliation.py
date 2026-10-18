"""Reconcile newly extracted decisions with the existing ledger.

Reconciliation only classifies. It never changes an existing decision's
status: a supersession or a conflict becomes an alert for a human to resolve.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from decision_ledger.domain.models import Decision, DecisionStatus
from decision_ledger.domain.ports import LLMClient
from decision_ledger.domain.rules import normalize_topic
from decision_ledger.services.drafts import DecisionDraft, Relation, RelationJudgement, decision_ref
from decision_ledger.services.structured import call_structured

_LIVE = (DecisionStatus.ACTIVE, DecisionStatus.UNDER_REVIEW)

PROMPT = """You maintain a ledger of team decisions. Classify how the NEW decision relates to the EXISTING decisions on the same topic.

Answer with exactly one relation:
- "merge": the new statement is the same decision restated (e.g. a confirmation). existing_id = that decision.
- "supersede": the new decision updates or replaces an existing conclusion on this topic, and the source states or clearly implies the change. existing_id = the replaced decision.
- "conflict": the new decision contradicts an existing decision without saying it replaces it. existing_id = the contradicted decision.
- "new": none of the above.

Keep explanation short and in the language of the decisions.

NEW decision:
topic: {topic}
statement: {statement}
rationale: {rationale}
decided_by: {decided_by}
quotes: {quotes}

EXISTING decisions:
{existing}
"""


def same_topic(a: str, b: str) -> bool:
    """Texts match when equal after normalising width, case and whitespace."""
    return normalize_topic(a) == normalize_topic(b)


@dataclass
class ReconcileOutcome:
    draft: DecisionDraft
    relation: Relation
    existing: Decision | None = None
    explanation: str = ""


class ReconciliationService:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def reconcile(
        self,
        drafts: Sequence[DecisionDraft],
        existing: Sequence[Decision],
        *,
        content_hash: str,
        topic_of: Mapping[int, str | None],
    ) -> list[ReconcileOutcome]:
        """Classify each draft as merge / supersede / conflict / new against live decisions in its topic.

        ``topic_of`` maps a draft index to the topic it was assigned to (None for a new topic).
        """
        live = [d for d in existing if d.status in _LIVE]
        return [self._classify(draft, live, content_hash, topic_of.get(draft.index)) for draft in drafts]

    def _classify(
        self, draft: DecisionDraft, live: Sequence[Decision], content_hash: str, topic_id: str | None
    ) -> ReconcileOutcome:
        candidates = {d.id: d for d in live if topic_id is not None and d.topic_id == topic_id}
        if not candidates:
            return ReconcileOutcome(draft, Relation.NEW)
        prompt = PROMPT.format(
            topic=draft.topic,
            statement=draft.statement,
            rationale=draft.rationale,
            decided_by=draft.decided_by,
            quotes=" / ".join(q.quote for q in draft.evidence),
            existing="\n".join(
                f"- #{d.id} [{d.status}] {d.statement} (rationale: {d.rationale}; by {d.decided_by})"
                for d in candidates.values()
            ),
        )
        judgement = call_structured(self.llm, prompt, RelationJudgement, key=f"reconcile:{content_hash}:{draft.index}")
        target = candidates.get(decision_ref(judgement.existing_id))
        if judgement.relation is Relation.NEW or target is None:
            return ReconcileOutcome(draft, Relation.NEW, explanation=judgement.explanation)
        return ReconcileOutcome(draft, judgement.relation, target, judgement.explanation)
