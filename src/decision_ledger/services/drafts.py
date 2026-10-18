"""LLM response models and verified drafts shared by the ingestion pipeline.

LLM output refers to source text by segment index + verbatim quote. Drafts
hold only quotes that were verified against the parsed segments; they become
domain ``Evidence`` once the source has an id inside the write transaction.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, Field

from decision_ledger.domain.models import CheckableRule, Evidence, Segment

# --- LLM response models -------------------------------------------------


class QuoteRef(BaseModel):
    segment_index: int
    quote: str


class ExtractedAssumption(BaseModel):
    text: str
    evidence: list[QuoteRef] = Field(default_factory=list)


class ExtractedRule(BaseModel):
    description: str
    path_patterns: list[str] = Field(default_factory=list)


class ExtractedDecision(BaseModel):
    topic: str
    statement: str
    rationale: str = ""
    decided_by: str = ""
    assumptions: list[ExtractedAssumption] = Field(default_factory=list)
    checkable_rule: ExtractedRule | None = None
    evidence: list[QuoteRef] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    decisions: list[ExtractedDecision] = Field(default_factory=list)


class Relation(StrEnum):
    MERGE = "merge"
    SUPERSEDE = "supersede"
    CONFLICT = "conflict"
    NEW = "new"


class RelationJudgement(BaseModel):
    relation: Relation = Relation.NEW
    existing_id: str | None = None
    explanation: str = ""


class AssumptionFinding(BaseModel):
    decision_id: str
    assumption_text: str
    evidence: list[QuoteRef] = Field(default_factory=list)
    explanation: str = ""


class AssumptionCheck(BaseModel):
    findings: list[AssumptionFinding] = Field(default_factory=list)


# --- verified drafts -----------------------------------------------------


class VerifiedQuote(BaseModel):
    segment_index: int
    locator: str
    speaker: str
    quote: str

    def to_evidence(self, source_id: str) -> Evidence:
        return Evidence(source_id=source_id, locator=self.locator, speaker=self.speaker, quote=self.quote)


class DraftAssumption(BaseModel):
    text: str
    evidence: list[VerifiedQuote] = Field(default_factory=list)


class DecisionDraft(BaseModel):
    """An extracted decision whose evidence has been verified against the source."""

    index: int
    topic: str
    statement: str
    rationale: str
    decided_by: str
    assumptions: list[DraftAssumption] = Field(default_factory=list)
    checkable_rule: CheckableRule | None = None
    evidence: list[VerifiedQuote] = Field(min_length=1)


_REF = re.compile(r"^\s*(?:decision\s*)?#?\s*(\w+)\s*$", re.IGNORECASE)


def decision_ref(raw: str | None) -> str:
    """Reduce an id the model wrote ("4", "#4", "decision #4") to the bare id ("4")."""
    match = _REF.match(raw or "")
    return match.group(1) if match else (raw or "").strip()


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\s\u300c\u300d\u300e\u300f\"'“”‘’]+", "", text)
    return text.casefold()


def verify_quotes(refs: Sequence[QuoteRef], segments: Sequence[Segment]) -> list[VerifiedQuote]:
    """Keep only quotes that occur in the source.

    The quote is matched (after normalising width, whitespace and quote marks)
    in the referenced segment first, then in any segment.
    """
    verified: list[VerifiedQuote] = []
    for ref in refs:
        needle = _normalize(ref.quote)
        if not needle:
            continue
        candidates = [s for s in segments if s.index == ref.segment_index] + list(segments)
        match = next((s for s in candidates if needle in _normalize(s.text)), None)
        if match is not None:
            verified.append(
                VerifiedQuote(segment_index=match.index, locator=match.locator, speaker=match.speaker, quote=ref.quote.strip())
            )
    return verified


def render_segments(segments: Sequence[Segment]) -> str:
    """Render segments for a prompt as ``[index] (locator) speaker: text`` lines."""
    return "\n".join(f"[{s.index}] ({s.locator}) {s.speaker}: {s.text}" for s in segments)
