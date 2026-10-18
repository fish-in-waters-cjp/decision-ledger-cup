"""Firestore implementation of the LedgerStore and Repository ports (DL-032).

Layout: ``projects/{pid}`` holds a project's lookup fields plus the full model as JSON; each entity lives in a
sub-collection of it (``sources``, ``decisions``, ``events``, ``alerts``, ``pr_checks``, ``topics``,
``counters``), and ``projects/{pid}/meta/version`` versions the project's writes. The global ``P<n>`` sequence
lives in ``ledger_meta/project_seq``.

A unit of work reads from Firestore with its own pending writes laid over the results, and on normal exit
commits every write in one ``WriteBatch`` together with ``meta/version + 1``. The version write carries a
``last_update_time`` precondition, so when another instance committed to the same project in the meantime
the whole batch fails and ``ConcurrentModification`` is raised; nothing is ever half-written or overwritten.
On an exception the pending writes are dropped. Id counters are part of the same batch, so they roll back too.

The SDK is the optional ``cloud`` extra: the query filter type, the conflict exceptions and the write option
come from the injected client or constructor arguments, so tests run on an in-memory fake.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from decision_ledger.domain.errors import (
    ConcurrentModification,
    DuplicatePrCheck,
    DuplicateSource,
    InvalidProjectOperation,
    NotFound,
    ParseError,
)
from decision_ledger.domain.models import (
    Alert,
    AlertStatus,
    Decision,
    DecisionEvent,
    DecisionStatus,
    Evidence,
    PrCheck,
    Project,
    ProjectSummary,
    Segment,
    Source,
    Topic,
)
from decision_ledger.domain.rules import normalize_topic

ROOT = "projects"
META = "ledger_meta"
SUBCOLLECTIONS = ("sources", "decisions", "events", "alerts", "pr_checks", "topics", "counters")
# A WriteBatch holds at most 500 writes; keep room for the version write and a safety margin.
MAX_WRITES = 450
# A Firestore document is at most 1 MiB; a source (with its segments) must stay well below that.
MAX_SOURCE_BYTES = 900_000
_EVENT_COUNTER = "__event"
_ATTEMPTS = 5


def _seq(value: str) -> int:
    digits = value.lstrip("P")
    return int(digits) if digits.isdigit() else 1 << 30


def _sort_key(fields: dict) -> tuple[int, str]:
    return (int(fields.get("seq", 1 << 30)), str(fields.get("id", "")))


def _pr_doc_id(key: str) -> str:
    """PR check keys contain "/" and "#", which a document id cannot; their sha256 is stable and unique."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _sdk_field_filter() -> Callable[..., Any]:  # pragma: no cover - needs the cloud extra
    from google.cloud.firestore_v1.base_query import FieldFilter

    return FieldFilter


def _sdk_conflict_errors() -> tuple[type[BaseException], ...]:  # pragma: no cover - needs the cloud extra
    from google.api_core import exceptions

    return (exceptions.Conflict, exceptions.FailedPrecondition, exceptions.Aborted)


# --- documents <-> models ----------------------------------------------------------


def _project_fields(project: Project) -> dict:
    return {
        "id": project.id,
        "seq": _seq(project.id),
        "name": project.name,
        "repo": project.repo,
        "owner_uid": project.owner_uid,
        "created_at": project.created_at.isoformat(),
        "data": project.model_dump_json(),
    }


def _source_fields(source: Source) -> dict:
    data = source.model_dump_json()
    if len(data.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ParseError(f"source is too large to store ({len(data.encode('utf-8'))} bytes; limit {MAX_SOURCE_BYTES})")
    return {"id": source.id, "seq": _seq(source.id), "content_hash": source.content_hash, "data": data}


def _decision_fields(decision: Decision, search_text: str) -> dict:
    return {
        "id": decision.id,
        "seq": _seq(decision.id),
        "topic_id": decision.topic_id,
        "status": decision.status.value,
        "checkable": decision.checkable_rule is not None,
        "search_text": search_text,
        "data": decision.model_dump_json(),
    }


def _alert_fields(alert: Alert) -> dict:
    return {"id": alert.id, "seq": _seq(alert.id), "status": alert.status.value, "data": alert.model_dump_json()}


def _topic_fields(topic: Topic) -> dict:
    return {
        "id": topic.id,
        "seq": _seq(topic.id),
        "norm_name": normalize_topic(topic.name),
        "merged_into": topic.merged_into,
        "data": topic.model_dump_json(),
    }


DIFF_TRUNCATED = "\n[diff truncated for storage: the pull request is larger than one stored record]\n"


def _fields_bytes(fields: dict) -> int:
    """The size the writer budgets for one document: field names plus their values as UTF-8 text."""
    return sum(len(str(k).encode("utf-8")) + len(str(v).encode("utf-8")) for k, v in fields.items())


def _pr_fields(check: PrCheck) -> dict:
    """A PR check keeps the full diff, which can exceed a document's 1 MiB limit; then the stored diff keeps its
    head (the deviation judge only ever reads the first ``diff_char_limit`` characters) and says it was cut."""
    def fields(c: PrCheck) -> dict:
        return {"key": c.key, "created_at": c.created_at.isoformat(), "data": c.model_dump_json()}

    out = fields(check)
    while _fields_bytes(out) > MAX_SOURCE_BYTES:  # the same measure _put applies, so a cut check always fits
        over = _fields_bytes(out) - MAX_SOURCE_BYTES
        raw = check.diff.removesuffix(DIFF_TRUNCATED).encode("utf-8")
        if not raw:
            raise ParseError(f"pull request check is too large to store (limit {MAX_SOURCE_BYTES} bytes)")
        head = raw[: max(0, len(raw) - over - len(DIFF_TRUNCATED) - 1024)].decode("utf-8", errors="ignore")
        head = head[: head.rfind("\n") + 1] if "\n" in head else head  # cut at a line boundary
        check = check.model_copy(update={"diff": head + DIFF_TRUNCATED})
        out = fields(check)
    return out


class FirestoreLedgerStore:
    """The database: projects, plus a project-bound repository for each. Implements ``LedgerStore``."""

    def __init__(
        self,
        client: Any,
        *,
        field_filter: Callable[..., Any] | None = None,
        conflict_errors: tuple[type[BaseException], ...] | None = None,
    ):
        self.client = client
        self._filter = field_filter or _sdk_field_filter()
        self.conflict_errors = conflict_errors or _sdk_conflict_errors()
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    # --- references -------------------------------------------------------

    def _projects(self) -> Any:
        return self.client.collection(ROOT)

    def project_ref(self, project_id: str) -> Any:
        return self._projects().document(project_id)

    def sub(self, project_id: str, name: str) -> Any:
        return self.project_ref(project_id).collection(name)

    def version_ref(self, project_id: str) -> Any:
        return self.project_ref(project_id).collection("meta").document("version")

    def lock(self, project_id: str) -> threading.RLock:
        """Serializes this process's units of work on one project (other instances are caught by the version)."""
        with self._locks_guard:
            return self._locks.setdefault(project_id, threading.RLock())

    def where(self, collection: Any, field: str, value: Any) -> Iterator[Any]:
        return collection.where(filter=self._filter(field, "==", value)).stream()

    # --- projects ---------------------------------------------------------

    def for_project(self, project_id: str) -> FirestoreRepository:
        return FirestoreRepository(self, project_id)

    def _name_ref(self, name: str) -> Any:
        """Claim document that keeps project names unique across instances (created with the project)."""
        return self.client.collection(META).document("names").collection("by_name").document(
            hashlib.sha256(name.encode("utf-8")).hexdigest()
        )

    def create_project(
        self, name: str, repo: str | None, project_id: str | None = None, owner_uid: str | None = None
    ) -> Project:
        for _ in range(_ATTEMPTS):
            if self._name_ref(name).get().exists:
                raise InvalidProjectOperation(f"project {name!r} already exists")
            if project_id is not None and self.project_ref(project_id).get().exists:
                raise InvalidProjectOperation(f"project {project_id!r} already exists")
            seq_ref = self.client.collection(META).document("project_seq")
            seq = seq_ref.get() if project_id is None else None
            pid = project_id or f"P{(seq.get('value') if seq.exists else 0) + 1}"
            project = Project(id=pid, name=name, repo=repo, owner_uid=owner_uid)
            batch = self.client.batch()
            batch.create(self.project_ref(pid), _project_fields(project))
            batch.create(self._name_ref(name), {"project_id": pid})
            if seq is not None:
                if seq.exists:
                    option = self.client.write_option(last_update_time=seq.update_time)
                    batch.update(seq_ref, {"value": seq.get("value") + 1}, option=option)
                else:
                    batch.create(seq_ref, {"value": 1})
            try:
                batch.commit()
                return project
            except self.conflict_errors:
                continue  # another instance won the name, the id or the next P<n>: the checks above decide
        raise ConcurrentModification("could not create the project; please retry")

    def list_projects(self, ids: Collection[str] | None = None) -> list[ProjectSummary]:
        if ids is None:
            snaps = self._projects().stream()
        else:  # read only the named projects: signed-in pages never pay for the whole site (DL-032)
            snaps = [s for s in (self.project_ref(i).get() for i in dict.fromkeys(ids)) if s.exists]
        docs = sorted((s.to_dict() for s in snaps), key=lambda f: (f["seq"], f["created_at"], f["id"]))
        summaries = []
        for fields in docs:
            project = Project.model_validate_json(fields["data"])
            pending = sum(1 for _ in self.where(self.sub(project.id, "alerts"), "status", AlertStatus.OPEN.value))
            summaries.append(ProjectSummary(**project.model_dump(), pending=pending))
        return summaries

    def get_project(self, project_id: str) -> Project | None:
        snap = self.project_ref(project_id).get()
        return Project.model_validate_json(snap.get("data")) if snap.exists else None

    def _projects_where(self, field: str, value: str) -> list[Project]:
        docs = sorted((s.to_dict() for s in self.where(self._projects(), field, value)), key=lambda f: (f["seq"], f["created_at"], f["id"]))
        return [Project.model_validate_json(f["data"]) for f in docs]

    def project_by_repo(self, repo: str) -> Project | None:
        projects = self.projects_by_repo(repo)
        return projects[0] if projects else None

    def projects_by_repo(self, repo: str) -> list[Project]:
        return self._projects_where("repo", repo)

    def project_by_owner(self, owner_uid: str) -> Project | None:
        projects = self._projects_where("owner_uid", owner_uid)
        return projects[0] if projects else None

    def update_project(self, project: Project) -> None:
        if not self.project_ref(project.id).get().exists:
            raise NotFound(f"project {project.id} not found")
        batch = self.client.batch()
        batch.update(self.project_ref(project.id), _project_fields(project))
        batch.commit()

    def reset_project(self, project_id: str) -> None:
        """Delete every entity of the project (in batches) and advance its version; the project stays."""
        with self.lock(project_id):
            refs = [snap.reference for name in SUBCOLLECTIONS for snap in self.sub(project_id, name).stream()]
            for start in range(0, len(refs), MAX_WRITES):
                batch = self.client.batch()
                for ref in refs[start : start + MAX_WRITES]:
                    batch.delete(ref)
                batch.commit()
            version = self.version_ref(project_id)
            snap = version.get()
            version.set({"version": (snap.get("version") if snap.exists else 0) + 1})


class FirestoreRepository:
    """The Repository port for one project: reads outside transactions see committed data only."""

    def __init__(self, store: FirestoreLedgerStore, project_id: str):
        self._store = store
        self.project_id = project_id

    @contextmanager
    def unit_of_work(self) -> Iterator[_FirestoreWriter]:
        with self._store.lock(self.project_id):
            writer = _FirestoreWriter(self._store, self.project_id)
            yield writer
            writer.commit()

    # --- helpers ----------------------------------------------------------

    def _doc(self, name: str, doc_id: str) -> dict | None:
        snap = self._store.sub(self.project_id, name).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def _all(self, name: str) -> list[dict]:
        return sorted((s.to_dict() for s in self._store.sub(self.project_id, name).stream()), key=_sort_key)

    def _where(self, name: str, field: str, value: Any) -> list[dict]:
        return sorted((s.to_dict() for s in self._store.where(self._store.sub(self.project_id, name), field, value)), key=_sort_key)

    # --- sources ----------------------------------------------------------

    def find_source_by_hash(self, content_hash: str) -> Source | None:
        docs = self._where("sources", "content_hash", content_hash)
        return Source.model_validate_json(docs[0]["data"]) if docs else None

    def get_source(self, source_id: str) -> Source | None:
        fields = self._doc("sources", source_id)
        return Source.model_validate_json(fields["data"]) if fields else None

    def list_sources(self) -> list[Source]:
        return [Source.model_validate_json(f["data"]) for f in self._all("sources")]

    def get_segments(self, source_id: str) -> list[Segment]:
        source = self.get_source(source_id)
        return sorted(source.segments, key=lambda s: s.index) if source else []

    # --- decisions --------------------------------------------------------

    def get_decision(self, decision_id: str) -> Decision | None:
        fields = self._doc("decisions", decision_id)
        return Decision.model_validate_json(fields["data"]) if fields else None

    def list_decisions(self, statuses: Sequence[DecisionStatus] | None = None) -> list[Decision]:
        wanted = None if statuses is None else {s.value for s in statuses}
        return [Decision.model_validate_json(f["data"]) for f in self._all("decisions") if wanted is None or f["status"] in wanted]

    def list_checkable_decisions(self) -> list[Decision]:
        return [
            Decision.model_validate_json(f["data"])
            for f in self._all("decisions")
            if f["checkable"] and f["status"] == DecisionStatus.ACTIVE.value
        ]

    def search_decisions(self, terms: Sequence[str], limit: int) -> list[Decision]:
        terms = list(dict.fromkeys(t.lower() for t in terms if t))
        if not terms:
            return []
        scored = []
        for f in self._all("decisions"):
            hits = sum(1 for t in terms if t in f["search_text"])
            if hits:
                scored.append((-hits, f["seq"], f["id"], f["data"]))
        scored.sort()
        return [Decision.model_validate_json(data) for *_, data in scored[:limit]]

    # --- alerts, events, PR checks ---------------------------------------

    def get_alert(self, alert_id: str) -> Alert | None:
        fields = self._doc("alerts", alert_id)
        return Alert.model_validate_json(fields["data"]) if fields else None

    def list_alerts(self, status: AlertStatus | None = None) -> list[Alert]:
        docs = self._all("alerts") if status is None else self._where("alerts", "status", status.value)
        return [Alert.model_validate_json(f["data"]) for f in docs]

    def list_events(self, decision_id: str) -> list[DecisionEvent]:
        docs = sorted(self._where("events", "decision_id", decision_id), key=lambda f: f["n"])
        return [DecisionEvent.model_validate_json(f["data"]) for f in docs]

    def get_pr_check(self, key: str) -> PrCheck | None:
        fields = self._doc("pr_checks", _pr_doc_id(key))
        return PrCheck.model_validate_json(fields["data"]) if fields else None

    def list_pr_checks(self) -> list[PrCheck]:
        docs = sorted((s.to_dict() for s in self._store.sub(self.project_id, "pr_checks").stream()), key=lambda f: f["created_at"])
        return [PrCheck.model_validate_json(f["data"]) for f in docs]

    # --- topics -----------------------------------------------------------

    def list_topics(self, include_merged: bool = False) -> list[Topic]:
        return [Topic.model_validate_json(f["data"]) for f in self._all("topics") if include_merged or f["merged_into"] is None]

    def get_topic(self, topic_id: str) -> Topic | None:
        fields = self._doc("topics", topic_id)
        return Topic.model_validate_json(fields["data"]) if fields else None

    def resolve_topic(self, topic_id: str) -> Topic | None:
        topic, seen = self.get_topic(topic_id), set()
        while topic is not None and topic.merged_into and topic.id not in seen:
            seen.add(topic.id)
            topic = self.get_topic(topic.merged_into)
        return topic

    def find_topic_by_name(self, name: str) -> Topic | None:
        norm = normalize_topic(name)
        docs = [f for f in self._where("topics", "norm_name", norm) if f["merged_into"] is None]
        return Topic.model_validate_json(docs[0]["data"]) if docs else None


class _FirestoreWriter:
    """Writes of one unit of work, buffered until commit; every read sees the buffered writes."""

    def __init__(self, store: FirestoreLedgerStore, project_id: str):
        self._store = store
        self.project_id = project_id
        self._pending: dict[tuple[str, str], dict | None] = {}  # (collection, doc id) -> fields, None = delete
        self._loaded: dict[str, dict[str, dict]] = {}  # committed documents of a fully read collection
        snap = store.version_ref(project_id).get()
        self._version = snap.get("version") if snap.exists else 0
        self._version_time = snap.update_time if snap.exists else None

    # --- buffered reads ---------------------------------------------------

    def _doc(self, name: str, doc_id: str) -> dict | None:
        if (name, doc_id) in self._pending:
            return self._pending[(name, doc_id)]
        if name in self._loaded:
            return self._loaded[name].get(doc_id)
        snap = self._store.sub(self.project_id, name).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def _all(self, name: str) -> list[dict]:
        """The collection as this unit of work sees it: committed documents with pending writes laid over."""
        if name not in self._loaded:
            self._loaded[name] = {s.id: s.to_dict() for s in self._store.sub(self.project_id, name).stream()}
        merged = dict(self._loaded[name])
        for (collection, doc_id), fields in self._pending.items():
            if collection != name:
                continue
            if fields is None:
                merged.pop(doc_id, None)
            else:
                merged[doc_id] = fields
        return sorted(merged.values(), key=_sort_key)

    def _put(self, name: str, doc_id: str, fields: dict | None) -> None:
        # Every record, not only sources and PR checks, must fit one Firestore document (1 MiB); an oversized one
        # (a huge topic name or note, evidence that kept growing) refuses the whole unit of work with 422, not 500.
        if fields is not None:
            size = _fields_bytes(fields)
            if size > MAX_SOURCE_BYTES:
                raise ParseError(f"a {name} record is too large to store ({size} bytes; limit {MAX_SOURCE_BYTES})")
        self._pending[(name, doc_id)] = fields

    # --- LedgerWriter -----------------------------------------------------

    def allocate_id(self, namespace: str) -> str:
        current = self._doc("counters", namespace)
        value = (current["value"] if current else 0) + 1
        self._put("counters", namespace, {"value": value})
        return str(value)

    def save_source(self, source: Source) -> None:
        source = source.model_copy(update={"project_id": self.project_id})
        if self._doc("sources", source.id) is not None or any(
            f["content_hash"] == source.content_hash for f in self._all("sources")
        ):
            raise DuplicateSource(source.content_hash)
        self._put("sources", source.id, _source_fields(source))

    def _search_text(self, decision: Decision) -> str:
        topic = self.get_topic(decision.topic_id)
        return f"{topic.name if topic else decision.topic} {decision.statement} {decision.rationale}".lower()

    def save_decision(self, decision: Decision) -> None:
        decision = decision.model_copy(update={"project_id": self.project_id})
        self._put("decisions", decision.id, _decision_fields(decision, self._search_text(decision)))

    def add_evidence(self, decision_id: str, evidence: Sequence[Evidence]) -> None:
        current = self.get_decision(decision_id)
        if current is not None:
            self.save_decision(current.model_copy(update={"evidence": [*current.evidence, *evidence]}))

    def get_decision(self, decision_id: str) -> Decision | None:
        fields = self._doc("decisions", decision_id)
        return Decision.model_validate_json(fields["data"]) if fields else None

    def save_alert(self, alert: Alert) -> None:
        alert = alert.model_copy(update={"project_id": self.project_id})
        self._put("alerts", alert.id, _alert_fields(alert))

    def get_alert_for_update(self, alert_id: str) -> Alert | None:
        # The project lock serializes this process; the version precondition catches other instances.
        fields = self._doc("alerts", alert_id)
        return Alert.model_validate_json(fields["data"]) if fields else None

    def obsolete_alerts_of(self, decision_ids: Sequence[str], except_alert_id: str | None) -> list[str]:
        targets = set(decision_ids)
        changed: list[str] = []
        for fields in self._all("alerts"):
            if fields["status"] != AlertStatus.OPEN.value:
                continue
            alert = Alert.model_validate_json(fields["data"])
            if alert.id != except_alert_id and targets & set(alert.decision_ids):
                self.save_alert(alert.model_copy(update={"status": AlertStatus.OBSOLETE}))
                changed.append(alert.id)
        return changed

    def append_event(self, event: DecisionEvent) -> None:
        number = int(self.allocate_id(_EVENT_COUNTER))
        stored = event.model_copy(update={"id": number})
        self._put("events", f"{number:012d}", {"n": number, "decision_id": event.decision_id, "data": stored.model_dump_json()})

    def save_pr_check(self, check: PrCheck) -> None:
        check = check.model_copy(update={"project_id": self.project_id})
        doc_id = _pr_doc_id(check.key)
        existing = self._doc("pr_checks", doc_id)
        if existing is not None and existing["created_at"] != check.created_at.isoformat():
            raise DuplicatePrCheck(check.key)
        self._put("pr_checks", doc_id, _pr_fields(check))

    # --- topics -----------------------------------------------------------

    def save_topic(self, topic: Topic) -> None:
        topic = topic.model_copy(update={"project_id": self.project_id})
        self._put("topics", topic.id, _topic_fields(topic))

    def get_topic(self, topic_id: str) -> Topic | None:
        fields = self._doc("topics", topic_id)
        return Topic.model_validate_json(fields["data"]) if fields else None

    def resolve_topic(self, topic_id: str) -> Topic | None:
        topic, seen = self.get_topic(topic_id), set()
        while topic is not None and topic.merged_into and topic.id not in seen:
            seen.add(topic.id)
            topic = self.get_topic(topic.merged_into)
        return topic

    def find_topic_by_name(self, name: str) -> Topic | None:
        norm = normalize_topic(name)
        docs = [f for f in self._all("topics") if f["norm_name"] == norm and f["merged_into"] is None]
        return Topic.model_validate_json(docs[0]["data"]) if docs else None

    def list_topic_decisions(self, topic_id: str) -> list[Decision]:
        return [Decision.model_validate_json(f["data"]) for f in self._all("decisions") if f["topic_id"] == topic_id]

    def _refresh_search_text(self, topic_id: str) -> None:
        for decision in self.list_topic_decisions(topic_id):
            self.save_decision(decision)

    def rename_topic(self, topic_id: str, name: str) -> None:
        topic = self.get_topic(topic_id)
        if topic is None:
            return
        self.save_topic(topic.model_copy(update={"name": name}))
        self._refresh_search_text(topic_id)

    def merge_topic(self, source_id: str, target_id: str) -> None:
        for decision in self.list_topic_decisions(source_id):
            self.save_decision(decision.model_copy(update={"topic_id": target_id}))
        # Repoint the source and every topic already merged into it, so chains stay one hop deep.
        for fields in self._all("topics"):
            if fields["id"] == source_id or fields["merged_into"] == source_id:
                topic = Topic.model_validate_json(fields["data"])
                self.save_topic(topic.model_copy(update={"merged_into": target_id}))

    # --- commit -----------------------------------------------------------

    def commit(self) -> None:
        """Write everything in one batch guarded by the project version; a read-only unit writes nothing."""
        if not self._pending:
            return
        if len(self._pending) > MAX_WRITES:
            raise ParseError(f"this change writes {len(self._pending)} documents; one import may write at most {MAX_WRITES}")
        store, client = self._store, self._store.client
        batch = client.batch()
        for (name, doc_id), fields in self._pending.items():
            ref = store.sub(self.project_id, name).document(doc_id)
            if fields is None:
                batch.delete(ref)
            else:
                batch.set(ref, fields)
        version = store.version_ref(self.project_id)
        if self._version_time is None:
            batch.create(version, {"version": 1})
        else:
            batch.update(version, {"version": self._version + 1}, option=client.write_option(last_update_time=self._version_time))
        try:
            batch.commit()
        except store.conflict_errors as exc:
            raise ConcurrentModification("another change was saved to this project at the same time; please retry") from exc
        self._pending.clear()
