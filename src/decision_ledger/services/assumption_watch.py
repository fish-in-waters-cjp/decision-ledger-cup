"""Watch the assumptions behind active decisions for contradicting new information."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from decision_ledger.domain.models import Assumption, Decision, DecisionStatus, DiscardedItem, Segment
from decision_ledger.domain.ports import LLMClient
from decision_ledger.services.drafts import AssumptionCheck, VerifiedQuote, decision_ref, render_segments, verify_quotes
from decision_ledger.services.reconciliation import same_topic
from decision_ledger.services.structured import call_structured

PROMPT = """Below are assumptions that active team decisions rely on, followed by new conversation segments.
Report every assumption that the NEW segments contradict or make doubtful. Ignore segments unrelated to any assumption.
For each finding give decision_id, the assumption_text exactly as listed, and evidence quotes copied verbatim from the segments (segment_index + quote).

ASSUMPTIONS:
{assumptions}

NEW SEGMENTS:
{segments}
"""


@dataclass
class AssumptionAlertDraft:
    decision: Decision
    assumption: Assumption
    new_evidence: list[VerifiedQuote]
    explanation: str


class AssumptionWatchService:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def check(
        self, segments: Sequence[Segment], existing: Sequence[Decision], *, content_hash: str
    ) -> tuple[list[AssumptionAlertDraft], list[DiscardedItem]]:
        """Return alert drafts for contradicted assumptions, plus findings discarded for lack of evidence."""
        watched = [d for d in existing if d.status is DecisionStatus.ACTIVE and d.assumptions]
        if not watched:
            return [], []
        prompt = PROMPT.format(
            assumptions="\n".join(f"- decision #{d.id} ({d.statement}): {a.text}" for d in watched for a in d.assumptions),
            segments=render_segments(segments),
        )
        check = call_structured(self.llm, prompt, AssumptionCheck, key=f"assumption:{content_hash}")
        by_id = {d.id: d for d in watched}

        alerts: list[AssumptionAlertDraft] = []
        discarded: list[DiscardedItem] = []
        for finding in check.findings:
            decision = by_id.get(decision_ref(finding.decision_id))
            assumption = next(
                (a for a in decision.assumptions if same_topic(a.text, finding.assumption_text)), None
            ) if decision else None
            quotes = verify_quotes(finding.evidence, segments)
            if decision is None or assumption is None:
                reason = "No matching decision or premise"
            elif not quotes:
                reason = "The quote for the new information was not found in the source"
            else:
                alerts.append(AssumptionAlertDraft(decision, assumption, quotes, finding.explanation))
                continue
            discarded.append(
                DiscardedItem(kind="assumption_alert", summary=f"#{finding.decision_id} {finding.assumption_text}", reason=reason)
            )
        return alerts, discarded
