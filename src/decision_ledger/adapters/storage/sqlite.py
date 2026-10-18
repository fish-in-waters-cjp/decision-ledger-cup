"""SQLite implementation of the LedgerStore and Repository ports.

One database holds every project. ``SqliteLedgerStore`` manages projects and
hands out ``ProjectScopedRepository`` objects bound to one project: every read
is filtered by ``project_id`` and every write is stamped with it, so services
cannot see another project's data. Ids are numbered per project, so every
table's key starts with ``project_id``.

Each table keeps the columns needed for lookups and constraints, plus the full
model as JSON. One connection is shared behind a lock (FastAPI runs sync
endpoints in a thread pool); writes use ``BEGIN IMMEDIATE`` transactions.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager

from decision_ledger.domain.errors import (
    DuplicatePrCheck,
    DuplicateSource,
    InvalidProjectOperation,
    NotFound,
    SchemaVersionMismatch,
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

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL UNIQUE,
    repo TEXT,
    data TEXT NOT NULL
);
CREATE TABLE topics (
    project_id TEXT NOT NULL REFERENCES projects (id),
    id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    norm_name TEXT NOT NULL,
    merged_into TEXT,
    data TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);
CREATE TABLE sources (
    project_id TEXT NOT NULL REFERENCES projects (id),
    id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (project_id, id),
    UNIQUE (project_id, content_hash)
);
CREATE TABLE segments (
    project_id TEXT NOT NULL REFERENCES projects (id),
    source_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    locator TEXT NOT NULL,
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (project_id, source_id, idx)
);
CREATE TABLE decisions (
    project_id TEXT NOT NULL REFERENCES projects (id),
    id TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    checkable INTEGER NOT NULL,
    search_text TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);
CREATE TABLE alerts (
    project_id TEXT NOT NULL REFERENCES projects (id),
    id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (project_id, id)
);
CREATE TABLE decision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects (id),
    decision_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE pr_checks (
    project_id TEXT NOT NULL REFERENCES projects (id),
    key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (project_id, key)
);
CREATE TABLE counters (
    project_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    value INTEGER NOT NULL,
    PRIMARY KEY (project_id, namespace)
);
CREATE INDEX idx_topics_name ON topics (project_id, norm_name);
CREATE INDEX idx_decisions_project ON decisions (project_id, topic_id);
CREATE INDEX idx_alerts_project ON alerts (project_id, status);
CREATE INDEX idx_events_decision ON decision_events (project_id, decision_id);
"""


# Version 3 → 4 (DL-032): a repo may be registered by several projects, so projects.repo loses UNIQUE.
# SQLite cannot drop a constraint: build the new table, copy, drop the old one, rename, with foreign keys
# off so the child tables keep pointing at "projects" (renaming the old table first would repoint them).
_MIGRATE_3_TO_4 = """
CREATE TABLE projects_v4 (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL UNIQUE,
    repo TEXT,
    data TEXT NOT NULL
);
INSERT INTO projects_v4 (id, seq, name, repo, data) SELECT id, seq, name, repo, data FROM projects;
DROP TABLE projects;
ALTER TABLE projects_v4 RENAME TO projects;
"""


def _seq(value: str) -> int:
    digits = value.lstrip("P")
    return int(digits) if digits.isdigit() else 1 << 30


# Project ids themselves come from a database-wide counter, kept under this empty project id.
_GLOBAL = ""


def _allocate(conn: sqlite3.Connection, namespace: str, project_id: str = _GLOBAL) -> str:
    """Next id in ``namespace`` for ``project_id``: every project numbers its own entities from 1."""
    conn.execute(
        "INSERT INTO counters (project_id, namespace, value) VALUES (?, ?, 1) "
        "ON CONFLICT(project_id, namespace) DO UPDATE SET value = value + 1",
        (project_id, namespace),
    )
    (value,) = conn.execute(
        "SELECT value FROM counters WHERE project_id = ? AND namespace = ?", (project_id, namespace)
    ).fetchone()
    return str(value)


class _Writer:
    """Write operations bound to the open transaction and to one project."""

    def __init__(self, conn: sqlite3.Connection, project_id: str):
        self.conn = conn
        self.project_id = project_id

    def _one(self, sql: str, params: Sequence) -> tuple | None:
        return self.conn.execute(sql, params).fetchone()

    def allocate_id(self, namespace: str) -> str:
        return _allocate(self.conn, namespace, self.project_id)

    def save_source(self, source: Source) -> None:
        source = source.model_copy(update={"project_id": self.project_id})
        try:
            self.conn.execute(
                "INSERT INTO sources (id, project_id, content_hash, data) VALUES (?, ?, ?, ?)",
                (source.id, self.project_id, source.content_hash, source.model_dump_json(exclude={"segments"})),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateSource(source.content_hash) from exc
        self.conn.executemany(
            "INSERT INTO segments (project_id, source_id, idx, locator, speaker, text) VALUES (?, ?, ?, ?, ?, ?)",
            [(self.project_id, source.id, s.index, s.locator, s.speaker, s.text) for s in source.segments],
        )

    def _search_text(self, decision: Decision) -> str:
        topic = self.get_topic(decision.topic_id)
        return f"{topic.name if topic else decision.topic} {decision.statement} {decision.rationale}".lower()

    def save_decision(self, decision: Decision) -> None:
        decision = decision.model_copy(update={"project_id": self.project_id})
        self.conn.execute(
            "INSERT INTO decisions (id, project_id, topic_id, seq, status, checkable, search_text, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, id) DO UPDATE SET topic_id = excluded.topic_id, status = excluded.status, "
            "checkable = excluded.checkable, search_text = excluded.search_text, data = excluded.data",
            (
                decision.id,
                self.project_id,
                decision.topic_id,
                _seq(decision.id),
                decision.status.value,
                int(decision.checkable_rule is not None),
                self._search_text(decision),
                decision.model_dump_json(),
            ),
        )

    def add_evidence(self, decision_id: str, evidence: Sequence[Evidence]) -> None:
        current = self.get_decision(decision_id)
        if current is not None:
            self.save_decision(current.model_copy(update={"evidence": [*current.evidence, *evidence]}))

    def get_decision(self, decision_id: str) -> Decision | None:
        row = self._one("SELECT data FROM decisions WHERE id = ? AND project_id = ?", (decision_id, self.project_id))
        return Decision.model_validate_json(row[0]) if row else None

    def save_alert(self, alert: Alert) -> None:
        alert = alert.model_copy(update={"project_id": self.project_id})
        self.conn.execute(
            "INSERT INTO alerts (id, project_id, seq, status, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, id) DO UPDATE SET status = excluded.status, data = excluded.data",
            (alert.id, self.project_id, _seq(alert.id), alert.status.value, alert.model_dump_json()),
        )

    def get_alert_for_update(self, alert_id: str) -> Alert | None:
        # BEGIN IMMEDIATE already holds the write lock for this transaction.
        row = self._one("SELECT data FROM alerts WHERE id = ? AND project_id = ?", (alert_id, self.project_id))
        return Alert.model_validate_json(row[0]) if row else None

    def obsolete_alerts_of(self, decision_ids: Sequence[str], except_alert_id: str | None) -> list[str]:
        targets = set(decision_ids)
        changed: list[str] = []
        rows = self.conn.execute(
            "SELECT data FROM alerts WHERE status = ? AND project_id = ? ORDER BY seq, id",
            (AlertStatus.OPEN.value, self.project_id),
        )
        for (data,) in rows.fetchall():
            alert = Alert.model_validate_json(data)
            if alert.id != except_alert_id and targets & set(alert.decision_ids):
                self.save_alert(alert.model_copy(update={"status": AlertStatus.OBSOLETE}))
                changed.append(alert.id)
        return changed

    def append_event(self, event: DecisionEvent) -> None:
        cursor = self.conn.execute(
            "INSERT INTO decision_events (project_id, decision_id, data) VALUES (?, ?, '')", (self.project_id, event.decision_id)
        )
        stored = event.model_copy(update={"id": cursor.lastrowid})
        self.conn.execute("UPDATE decision_events SET data = ? WHERE id = ?", (stored.model_dump_json(), cursor.lastrowid))

    def save_pr_check(self, check: PrCheck) -> None:
        check = check.model_copy(update={"project_id": self.project_id})
        created = check.created_at.isoformat()
        try:
            self.conn.execute(
                "INSERT INTO pr_checks (project_id, key, created_at, data) VALUES (?, ?, ?, ?)",
                (self.project_id, check.key, created, check.model_dump_json()),
            )
        except sqlite3.IntegrityError as exc:
            row = self._one("SELECT created_at FROM pr_checks WHERE project_id = ? AND key = ?", (self.project_id, check.key))
            if row is None or row[0] != created:
                raise DuplicatePrCheck(check.key) from exc
            self.conn.execute(
                "UPDATE pr_checks SET data = ? WHERE project_id = ? AND key = ?",
                (check.model_dump_json(), self.project_id, check.key),
            )

    # --- topics ------------------------------------------------------------

    def save_topic(self, topic: Topic) -> None:
        topic = topic.model_copy(update={"project_id": self.project_id})
        self.conn.execute(
            "INSERT INTO topics (id, project_id, seq, norm_name, merged_into, data) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, id) DO UPDATE SET norm_name = excluded.norm_name, merged_into = excluded.merged_into, "
            "data = excluded.data",
            (topic.id, self.project_id, _seq(topic.id), normalize_topic(topic.name), topic.merged_into, topic.model_dump_json()),
        )

    def get_topic(self, topic_id: str) -> Topic | None:
        return _get_topic(self.conn, self.project_id, topic_id)

    def resolve_topic(self, topic_id: str) -> Topic | None:
        return _resolve_topic(self.conn, self.project_id, topic_id)

    def find_topic_by_name(self, name: str) -> Topic | None:
        return _find_topic_by_name(self.conn, self.project_id, name)

    def list_topic_decisions(self, topic_id: str) -> list[Decision]:
        rows = self.conn.execute(
            "SELECT data FROM decisions WHERE topic_id = ? AND project_id = ? ORDER BY seq, id", (topic_id, self.project_id)
        ).fetchall()
        return [Decision.model_validate_json(d) for (d,) in rows]

    def _refresh_search_text(self, topic_id: str) -> None:
        rows = self.conn.execute(
            "SELECT data FROM decisions WHERE topic_id = ? AND project_id = ?", (topic_id, self.project_id)
        ).fetchall()
        for (data,) in rows:
            self.save_decision(Decision.model_validate_json(data))

    def rename_topic(self, topic_id: str, name: str) -> None:
        topic = self.get_topic(topic_id)
        if topic is None:
            return
        self.save_topic(topic.model_copy(update={"name": name}))
        self._refresh_search_text(topic_id)

    def merge_topic(self, source_id: str, target_id: str) -> None:
        rows = self.conn.execute(
            "SELECT data FROM decisions WHERE topic_id = ? AND project_id = ?", (source_id, self.project_id)
        ).fetchall()
        for (data,) in rows:
            self.save_decision(Decision.model_validate_json(data).model_copy(update={"topic_id": target_id}))
        # Repoint the source and every topic already merged into it, so chains stay one hop deep.
        rows = self.conn.execute(
            "SELECT data FROM topics WHERE project_id = ? AND (id = ? OR merged_into = ?)",
            (self.project_id, source_id, source_id),
        ).fetchall()
        for (data,) in rows:
            self.save_topic(Topic.model_validate_json(data).model_copy(update={"merged_into": target_id}))


def _get_topic(conn: sqlite3.Connection, project_id: str, topic_id: str) -> Topic | None:
    row = conn.execute("SELECT data FROM topics WHERE id = ? AND project_id = ?", (topic_id, project_id)).fetchone()
    return Topic.model_validate_json(row[0]) if row else None


def _resolve_topic(conn: sqlite3.Connection, project_id: str, topic_id: str) -> Topic | None:
    topic, seen = _get_topic(conn, project_id, topic_id), set()
    while topic is not None and topic.merged_into and topic.id not in seen:
        seen.add(topic.id)
        topic = _get_topic(conn, project_id, topic.merged_into)
    return topic


def _find_topic_by_name(conn: sqlite3.Connection, project_id: str, name: str) -> Topic | None:
    row = conn.execute(
        "SELECT data FROM topics WHERE project_id = ? AND norm_name = ? AND merged_into IS NULL ORDER BY seq LIMIT 1",
        (project_id, normalize_topic(name)),
    ).fetchone()
    return Topic.model_validate_json(row[0]) if row else None


class SqliteLedgerStore:
    """The database: projects, plus a project-bound repository for each."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            # Read the version before running any DDL: an incompatible database must stay untouched.
            (version,) = self._conn.execute("PRAGMA user_version").fetchone()
            if version == 0:
                self._conn.executescript(f"BEGIN; {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
            elif version == 3:
                self._conn.execute("PRAGMA foreign_keys = OFF")
                self._conn.executescript(f"BEGIN; {_MIGRATE_3_TO_4} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
            elif version != SCHEMA_VERSION:
                raise SchemaVersionMismatch(path, version, SCHEMA_VERSION)
        except BaseException:
            self._conn.close()
            raise
        self._conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def rows(self, sql: str, params: Sequence = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # UsageCounter (DL-021): reuse the counters table under a "usage:<scope>" owner, apart from project ids.
    def increment(self, scope: str, key: str) -> int:
        with self.transaction() as conn:
            return int(_allocate(conn, key, f"usage:{scope}"))

    def get(self, scope: str, key: str) -> int:
        rows = self.rows("SELECT value FROM counters WHERE project_id = ? AND namespace = ?", (f"usage:{scope}", key))
        return int(rows[0][0]) if rows else 0

    def for_project(self, project_id: str) -> ProjectScopedRepository:
        return ProjectScopedRepository(self, project_id)

    def create_project(
        self, name: str, repo: str | None, project_id: str | None = None, owner_uid: str | None = None
    ) -> Project:
        with self.transaction() as conn:
            project = Project(id=project_id or "P" + _allocate(conn, "project"), name=name, repo=repo, owner_uid=owner_uid)
            try:
                conn.execute(
                    "INSERT INTO projects (id, seq, name, repo, data) VALUES (?, ?, ?, ?, ?)",
                    (project.id, _seq(project.id), name, repo, project.model_dump_json()),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidProjectOperation(f"project {project.id!r} or {name!r} already exists") from exc
        return project

    def reset_project(self, project_id: str) -> None:
        """Delete all of the project's rows in one transaction; the project itself stays."""
        with self.transaction() as conn:
            for table in ("segments", "decision_events", "alerts", "pr_checks", "decisions", "sources", "topics", "counters"):
                conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))

    def list_projects(self, ids: Collection[str] | None = None) -> list[ProjectSummary]:
        wanted = list(dict.fromkeys(ids)) if ids is not None else None
        where = f"WHERE p.id IN ({', '.join('?' * len(wanted))}) " if wanted else ("WHERE 0 " if wanted is not None else "")
        rows = self.rows(
            "SELECT p.data, (SELECT COUNT(*) FROM alerts a WHERE a.project_id = p.id AND a.status = ?) "
            f"FROM projects p {where}ORDER BY p.seq",
            (AlertStatus.OPEN.value, *(wanted or [])),
        )
        return [ProjectSummary(**Project.model_validate_json(data).model_dump(), pending=n) for data, n in rows]

    def get_project(self, project_id: str) -> Project | None:
        rows = self.rows("SELECT data FROM projects WHERE id = ?", (project_id,))
        return Project.model_validate_json(rows[0][0]) if rows else None

    def project_by_repo(self, repo: str) -> Project | None:
        projects = self.projects_by_repo(repo)
        return projects[0] if projects else None

    def projects_by_repo(self, repo: str) -> list[Project]:
        rows = self.rows("SELECT data FROM projects WHERE repo = ? ORDER BY seq, id", (repo,))
        return [Project.model_validate_json(d) for (d,) in rows]

    def project_by_owner(self, owner_uid: str) -> Project | None:
        # owner_uid lives in the JSON; the projects table is small (one row per workspace).
        for (data,) in self.rows("SELECT data FROM projects ORDER BY seq, id"):
            project = Project.model_validate_json(data)
            if project.owner_uid == owner_uid:
                return project
        return None

    def update_project(self, project: Project) -> None:
        with self.transaction() as conn:
            updated = conn.execute(
                "UPDATE projects SET repo = ?, data = ? WHERE id = ?", (project.repo, project.model_dump_json(), project.id)
            ).rowcount
        if not updated:
            raise NotFound(f"project {project.id} not found")


class ProjectScopedRepository:
    """The Repository port for one project. It cannot reach another project."""

    def __init__(self, store: SqliteLedgerStore, project_id: str):
        self._store = store
        self.project_id = project_id

    @contextmanager
    def unit_of_work(self) -> Iterator[_Writer]:
        with self._store.transaction() as conn:
            yield _Writer(conn, self.project_id)

    def _rows(self, sql: str, params: Sequence = ()) -> list[tuple]:
        return self._store.rows(sql, (*params, self.project_id))

    # --- sources -----------------------------------------------------------

    def _source(self, data: str) -> Source:
        source = Source.model_validate_json(data)
        return source.model_copy(update={"segments": self.get_segments(source.id)})

    def find_source_by_hash(self, content_hash: str) -> Source | None:
        rows = self._rows("SELECT data FROM sources WHERE content_hash = ? AND project_id = ?", (content_hash,))
        return self._source(rows[0][0]) if rows else None

    def get_source(self, source_id: str) -> Source | None:
        rows = self._rows("SELECT data FROM sources WHERE id = ? AND project_id = ?", (source_id,))
        return self._source(rows[0][0]) if rows else None

    def list_sources(self) -> list[Source]:
        return [self._source(d) for (d,) in self._rows("SELECT data FROM sources WHERE project_id = ? ORDER BY rowid")]

    def get_segments(self, source_id: str) -> list[Segment]:
        rows = self._rows(
            "SELECT idx, locator, speaker, text FROM segments WHERE source_id = ? AND project_id = ? ORDER BY idx",
            (source_id,),
        )
        return [Segment(index=i, locator=loc, speaker=who, text=text) for i, loc, who, text in rows]

    # --- decisions ---------------------------------------------------------

    def get_decision(self, decision_id: str) -> Decision | None:
        rows = self._rows("SELECT data FROM decisions WHERE id = ? AND project_id = ?", (decision_id,))
        return Decision.model_validate_json(rows[0][0]) if rows else None

    def list_decisions(self, statuses: Sequence[DecisionStatus] | None = None) -> list[Decision]:
        if statuses is None:
            rows = self._rows("SELECT data FROM decisions WHERE project_id = ? ORDER BY seq, id")
        else:
            marks = ",".join("?" * len(statuses))
            rows = self._rows(
                f"SELECT data FROM decisions WHERE status IN ({marks}) AND project_id = ? ORDER BY seq, id",
                [s.value for s in statuses],
            )
        return [Decision.model_validate_json(d) for (d,) in rows]

    def list_checkable_decisions(self) -> list[Decision]:
        rows = self._rows(
            "SELECT data FROM decisions WHERE checkable = 1 AND status = ? AND project_id = ? ORDER BY seq, id",
            (DecisionStatus.ACTIVE.value,),
        )
        return [Decision.model_validate_json(d) for (d,) in rows]

    def search_decisions(self, terms: Sequence[str], limit: int) -> list[Decision]:
        # Scored in Python: one LIKE per term in SQL hits SQLite's expression-depth limit on long
        # questions, and capping the term count drops late key terms. Ledgers here are small.
        terms = list(dict.fromkeys(t.lower() for t in terms if t))
        if not terms:
            return []
        scored = []
        for seq, search_text, data in self._rows("SELECT seq, search_text, data FROM decisions WHERE project_id = ?"):
            hits = sum(1 for t in terms if t in search_text)
            if hits:
                scored.append((-hits, seq, data))
        scored.sort()
        return [Decision.model_validate_json(data) for _, _, data in scored[:limit]]

    # --- alerts, events, PR checks ----------------------------------------

    def get_alert(self, alert_id: str) -> Alert | None:
        rows = self._rows("SELECT data FROM alerts WHERE id = ? AND project_id = ?", (alert_id,))
        return Alert.model_validate_json(rows[0][0]) if rows else None

    def list_alerts(self, status: AlertStatus | None = None) -> list[Alert]:
        if status is None:
            rows = self._rows("SELECT data FROM alerts WHERE project_id = ? ORDER BY seq, id")
        else:
            rows = self._rows("SELECT data FROM alerts WHERE status = ? AND project_id = ? ORDER BY seq, id", (status.value,))
        return [Alert.model_validate_json(d) for (d,) in rows]

    def list_events(self, decision_id: str) -> list[DecisionEvent]:
        rows = self._rows(
            "SELECT data FROM decision_events WHERE decision_id = ? AND project_id = ? ORDER BY id",
            (decision_id,),
        )
        return [DecisionEvent.model_validate_json(d) for (d,) in rows]

    def get_pr_check(self, key: str) -> PrCheck | None:
        rows = self._rows("SELECT data FROM pr_checks WHERE key = ? AND project_id = ?", (key,))
        return PrCheck.model_validate_json(rows[0][0]) if rows else None

    def list_pr_checks(self) -> list[PrCheck]:
        rows = self._rows("SELECT data FROM pr_checks WHERE project_id = ? ORDER BY created_at")
        return [PrCheck.model_validate_json(d) for (d,) in rows]

    # --- topics ------------------------------------------------------------

    def list_topics(self, include_merged: bool = False) -> list[Topic]:
        where = "" if include_merged else " AND merged_into IS NULL"
        rows = self._rows(f"SELECT data FROM topics WHERE project_id = ?{where} ORDER BY seq, id")
        return [Topic.model_validate_json(d) for (d,) in rows]

    def get_topic(self, topic_id: str) -> Topic | None:
        with self._store._lock:
            return _get_topic(self._store._conn, self.project_id, topic_id)

    def resolve_topic(self, topic_id: str) -> Topic | None:
        with self._store._lock:
            return _resolve_topic(self._store._conn, self.project_id, topic_id)

    def find_topic_by_name(self, name: str) -> Topic | None:
        with self._store._lock:
            return _find_topic_by_name(self._store._conn, self.project_id, name)
