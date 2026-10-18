"""Topics: assign new decisions to topics, and let people rename or merge them.

Topic changes only move decisions between topics. They never change a
decision's status and never raise alerts (Human-in-the-loop).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from decision_ledger.domain.errors import InvalidTopicOperation, LLMOutputError, NotFound
from decision_ledger.domain.models import Topic
from decision_ledger.domain.ports import LLMClient, Repository
from decision_ledger.domain.rules import normalize_topic
from decision_ledger.services.drafts import DecisionDraft
from decision_ledger.services.structured import call_structured
from decision_ledger.services.drafts import decision_ref

PROMPT = """You group team decisions by subject. For each NEW decision, pick the EXISTING topic about the same subject,
even when it is worded differently (e.g. "DB choice" and "database selection" are the same subject).
Return topic_id null when no existing topic is about the same subject.

NEW decisions:
{drafts}

EXISTING topics:
{topics}
"""


class TopicPick(BaseModel):
    index: int
    topic_id: str | None = None


class TopicAssignment(BaseModel):
    """LLM output; anything missing means "create a new topic"."""

    assignments: list[TopicPick] = Field(default_factory=list)


@dataclass(frozen=True)
class TopicChoice:
    """Where a draft goes: an existing topic id, or None to create a topic named ``name``."""

    topic_id: str | None
    name: str


class TopicAssigner:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def assign(self, drafts: Sequence[DecisionDraft], topics: Sequence[Topic], *, content_hash: str) -> list[TopicChoice]:
        """Choose a topic per draft: exact (normalized) name first, else the LLM; a new topic when unsure.

        ``topics`` may include merged topics: a draft using a merged topic's name goes to the topic that
        absorbed it, so a merge is never undone by a later import.
        """
        everything = {t.id: t for t in topics}
        live = {t.id: t for t in topics if t.merged_into is None}
        by_name = {
            normalize_topic(t.name): target
            for t in topics
            if t.merged_into is not None and (target := _final(t, everything)) is not None
        }
        by_name.update({normalize_topic(t.name): t for t in live.values()})
        choices: dict[int, TopicChoice] = {}
        pending: list[DecisionDraft] = []
        for draft in drafts:
            exact = by_name.get(normalize_topic(draft.topic))
            if exact is not None:
                choices[draft.index] = TopicChoice(exact.id, exact.name)
            else:
                pending.append(draft)

        picks: dict[int, str] = {}
        if pending and live:
            try:
                result = call_structured(
                    self.llm,
                    PROMPT.format(
                        drafts="\n".join(f"- [{d.index}] topic: {d.topic}; {d.statement}" for d in pending),
                        topics="\n".join(f"- {t.id}: {t.name}" for t in live.values()),
                    ),
                    TopicAssignment,
                    key=f"topic:{content_hash}",
                )
                picks = {p.index: decision_ref(p.topic_id) for p in result.assignments if p.topic_id}
            except LLMOutputError:
                picks = {}
        for draft in pending:
            target = live.get(picks.get(draft.index, ""))
            choices[draft.index] = TopicChoice(target.id, target.name) if target else TopicChoice(None, draft.topic.strip())
        return [choices[d.index] for d in drafts]


def _final(topic: Topic, topics: dict[str, Topic]) -> Topic | None:
    """Follow ``merged_into`` to the live topic, or None when the chain leaves the given topics."""
    seen: set[str] = set()
    while topic.merged_into is not None and topic.id not in seen:
        seen.add(topic.id)
        nxt = topics.get(topic.merged_into)
        if nxt is None:
            return None
        topic = nxt
    return topic if topic.merged_into is None else None


class TopicService:
    """Rename and merge topics. Only membership changes: no status change, no alert."""

    def __init__(self, repo: Repository):
        self.repo = repo

    def rename(self, topic_id: str, name: str) -> Topic:
        """Rename a topic; the name must be non-blank and not used by another topic of this project."""
        name = name.strip()
        with self.repo.unit_of_work() as writer:
            topic = writer.get_topic(topic_id)
            if topic is None:
                raise NotFound(f"topic {topic_id} not found")
            if topic.merged_into is not None:
                raise InvalidTopicOperation(f"Topic {topic_id} was merged and cannot be renamed")
            if not name:
                raise InvalidTopicOperation("Topic name cannot be blank")
            other = writer.find_topic_by_name(name)
            if other is not None and other.id != topic.id:
                if writer.list_topic_decisions(other.id):
                    raise InvalidTopicOperation(f"A topic named \"{other.name}\" already exists; merge instead")
                # A hidden, emptied topic must not block the name: fold it into this one (it holds no decisions).
                writer.merge_topic(other.id, topic.id)
            writer.rename_topic(topic.id, name)
        return topic.model_copy(update={"name": name})

    def merge(self, topic_id: str, into: str) -> Topic:
        """Move every decision of ``topic_id`` into ``into`` and return the target topic."""
        with self.repo.unit_of_work() as writer:
            source = writer.get_topic(topic_id)
            if source is None:
                raise NotFound(f"topic {topic_id} not found")
            target = writer.get_topic(into)
            if target is None:
                raise InvalidTopicOperation(f"Target topic {into} does not exist in this project")
            if target.id == source.id:
                raise InvalidTopicOperation("A topic cannot be merged into itself")
            if source.merged_into is not None or target.merged_into is not None:
                raise InvalidTopicOperation("A merged topic cannot be merged again")
            writer.merge_topic(source.id, target.id)
        return target

    def move(self, topic_id: str, decision_ids: Sequence[str], into: str) -> Topic:
        """Move decisions of ``topic_id`` into another live topic of this project and return that topic."""
        ids = list(dict.fromkeys(decision_ids))
        with self.repo.unit_of_work() as writer:
            source = self._live_source(writer, topic_id)
            target = writer.get_topic(into)
            if target is None:
                raise InvalidTopicOperation(f"Target topic {into} does not exist in this project")
            if target.id == source.id:
                raise InvalidTopicOperation("The decisions are already in that topic")
            if target.merged_into is not None:
                raise InvalidTopicOperation("Decisions cannot move to a merged topic")
            self._reassign(writer, self._members(writer, source, ids), target)
        return target

    def split(self, topic_id: str, decision_ids: Sequence[str], name: str) -> Topic:
        """Move some decisions of ``topic_id`` into a new topic named ``name`` and return it."""
        ids, name = list(dict.fromkeys(decision_ids)), name.strip()
        with self.repo.unit_of_work() as writer:
            source = self._live_source(writer, topic_id)
            if not name:
                raise InvalidTopicOperation("New topic name cannot be blank")
            decisions = self._members(writer, source, ids)
            if {d.id for d in decisions} >= {d.id for d in writer.list_topic_decisions(source.id)}:
                raise InvalidTopicOperation("Splitting out every decision of a topic is a rename; rename instead")
            target = writer.find_topic_by_name(name)
            if target is not None and writer.list_topic_decisions(target.id):
                raise InvalidTopicOperation(f"A topic named \"{name}\" already exists; move the decisions instead")
            if target is None:
                target = Topic(id=writer.allocate_id("topic"), name=name)
                writer.save_topic(target)
            # else: reuse the hidden, emptied topic that already owns this name.
            self._reassign(writer, decisions, target)
        return target

    @staticmethod
    def _live_source(writer, topic_id: str) -> Topic:
        source = writer.get_topic(topic_id)
        if source is None:
            raise NotFound(f"topic {topic_id} not found")
        if source.merged_into is not None:
            raise InvalidTopicOperation(f"Topic {topic_id} was merged")
        return source

    @staticmethod
    def _members(writer, source: Topic, ids: list[str]) -> list:
        """The selected decisions, each required to exist in ``source`` (NotFound otherwise)."""
        if not ids:
            raise InvalidTopicOperation("Select at least one decision")
        decisions = [writer.get_decision(i) for i in ids]
        for decision_id, decision in zip(ids, decisions, strict=True):
            if decision is None or decision.topic_id != source.id:
                raise NotFound(f"decision {decision_id} not found in topic {source.id}")
        return decisions

    @staticmethod
    def _reassign(writer, decisions: list, target: Topic) -> None:
        """Only membership changes: the decisions keep their status and no alert is raised."""
        for decision in decisions:
            writer.save_decision(decision.model_copy(update={"topic_id": target.id}))
