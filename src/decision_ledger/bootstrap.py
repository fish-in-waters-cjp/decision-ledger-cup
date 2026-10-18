"""Composition root: the only module that picks concrete backends.

Every choice comes from environment variables, so switching between Fake,
Gemini and Claude Code (or SQLite paths) never touches business code.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import uvicorn

from decision_ledger.adapters.github import DryRunCommentPublisher, GitHubPullRequestSource
from decision_ledger.adapters.llm.claude_code import ClaudeCodeLLM
from decision_ledger.adapters.llm.fake import FakeReplayLLM
from decision_ledger.adapters.llm.recording import RecordingLLM
from decision_ledger.adapters.memory import InMemorySandboxRegistry
from decision_ledger.adapters.storage.sqlite import SqliteLedgerStore
from decision_ledger.domain.errors import NotFound
from decision_ledger.domain.models import AlertKind, AlertStatus, Project, PullRequestDiff, SourceKind
from decision_ledger.domain.ports import (
    CommentPublisher,
    IdentityVerifier,
    LedgerStore,
    LLMClient,
    PullRequestSource,
    SandboxRegistry,
    UsageCounter,
)
from decision_ledger.services.alerts import AlertService
from decision_ledger.services.assumption_watch import AssumptionWatchService
from decision_ledger.services.container import Container, RuntimeInfo, ShowcaseState
from decision_ledger.services.deviation import DeviationService
from decision_ledger.services.ingestion import IngestionService
from decision_ledger.services.projects import ProjectService
from decision_ledger.services.parsers import content_hash
from decision_ledger.services.qa import LedgerQuery, QAService
from decision_ledger.services.reconciliation import ReconciliationService
from decision_ledger.services.sandboxes import SandboxService
from decision_ledger.services.topics import TopicAssigner, TopicService
from decision_ledger.services.usage import LimitedLLM

log = logging.getLogger(__name__)

DEMO_DIR = Path(__file__).resolve().parent / "demo" / "fixtures"
log = logging.getLogger(__name__)
BACKENDS = ("fake", "gemini", "claude_code")


# Judging ends and results may be revisited until Demo Day; a model must outlive this day.
JUDGING_CUTOFF = date(2026, 12, 4)


@dataclass(frozen=True)
class GeminiModelInfo:
    """Official Vertex AI retirement date; ``or_later`` marks a published lower bound, not a fixed day."""

    retires_on: date
    or_later: bool = False


# Allow-list: Vertex AI "Model versions" page, checked 2026-10-17; re-check by eye before submission (10/18)
# (docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions). Re-check before submission.
GEMINI_MODEL_RETIREMENTS: dict[str, GeminiModelInfo] = {
    "gemini-3.5-flash": GeminiModelInfo(date(2027, 5, 19), or_later=True),
    "gemini-3.7-flash": GeminiModelInfo(date(2027, 1, 28)),
    "gemini-3.8-flash": GeminiModelInfo(date(2027, 9, 2), or_later=True),
}
# Known models that retire too early; listed only so the refusal can name the date.
KNOWN_RETIRED_GEMINI_MODELS: dict[str, date] = {"gemini-2.5-flash": date(2026, 10, 20), "gemini-3.6-flash": date(2026, 11, 19)}


class ModelNotAllowed(ValueError):
    """The configured Gemini model is not allowed for this deployment."""


def check_gemini_model(model: str) -> None:
    """Raise ``ModelNotAllowed`` unless ``model`` is allow-listed and retires after ``JUDGING_CUTOFF``."""
    allowed = ", ".join(sorted(GEMINI_MODEL_RETIREMENTS))
    info = GEMINI_MODEL_RETIREMENTS.get(model)
    if info is None:
        retired = KNOWN_RETIRED_GEMINI_MODELS.get(model)
        when = f"retires {retired.isoformat()}" if retired else "is not on the allow-list"
        raise ModelNotAllowed(
            f"DL_GEMINI_MODEL={model} {when}; it must retire after {JUDGING_CUTOFF.isoformat()}. Allowed: {allowed}"
        )
    if info.retires_on <= JUDGING_CUTOFF:
        raise ModelNotAllowed(
            f"DL_GEMINI_MODEL={model} retires {info.retires_on.isoformat()}; it must retire after "
            f"{JUDGING_CUTOFF.isoformat()}. Allowed: {allowed}"
        )


def _bool(value: str | None, default: bool) -> bool:
    return default if value is None else value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    llm_backend: str = "fake"
    db_path: str = "decision-ledger.db"
    webhook_secret: str | None = field(default=None, repr=False)  # secret: never printed
    llm_timeout: float = 60.0
    diff_char_limit: int = 20_000
    github_token: str | None = field(default=None, repr=False)  # secret: never printed
    github_dry_run: bool = True
    gemini_model: str = "gemini-3.5-flash"
    gemini_api_key: str | None = field(default=None, repr=False)  # secret: never printed
    gemini_vertexai: bool = False
    gemini_access_token: str | None = field(default=None, repr=False)  # secret: never printed
    llm_record_path: str | None = None
    gcp_project: str | None = None
    gcp_location: str = "global"
    claude_command: str = "claude"
    fixtures_dir: str = str(DEMO_DIR / "llm")
    project_name: str = "Local Ledger"
    project_repo: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    recorded_mode: bool = False
    cors_origins: tuple[str, ...] = ()
    public_url: str | None = None
    sandbox_salt: str | None = None
    api_base: str | None = None
    public_mode: bool = False
    firestore: bool = False
    showcase_rebuild: bool = False
    sandbox_daily_llm_limit: int = 60
    site_daily_llm_limit: int = 1500
    sandbox_daily_create_limit: int = 100
    # DL-032: where the ledger lives and how users sign in.
    store: str = "sqlite"
    auth_mode: str = "none"
    firebase_project_id: str | None = None
    firebase_api_key: str | None = field(default=None, repr=False)  # public web key, still never printed
    firebase_auth_domain: str | None = None
    daily_new_user_limit: int = 100

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings from ``DL_*`` (and standard Google) environment variables."""
        env = os.environ if env is None else env
        store = (env.get("DL_STORE") or cls.store).strip().lower()
        firebase_project = (env.get("DL_FIREBASE_PROJECT_ID") or "").strip() or None
        return cls(
            store=store,
            auth_mode=(env.get("DL_AUTH_MODE") or cls.auth_mode).strip().lower(),
            firebase_project_id=firebase_project,
            firebase_api_key=env.get("DL_FIREBASE_API_KEY") or None,
            firebase_auth_domain=(env.get("DL_FIREBASE_AUTH_DOMAIN") or "").strip()
            or (f"{firebase_project}.firebaseapp.com" if firebase_project else None),
            daily_new_user_limit=int(env.get("DL_DAILY_NEW_USER_LIMIT", cls.daily_new_user_limit)),
            llm_backend=env.get("DL_LLM_BACKEND", "fake").strip().lower(),
            db_path=env.get("DL_DB_PATH", cls.db_path),
            webhook_secret=env.get("DL_WEBHOOK_SECRET") or None,
            llm_timeout=float(env.get("DL_LLM_TIMEOUT", cls.llm_timeout)),
            diff_char_limit=int(env.get("DL_DIFF_CHAR_LIMIT", cls.diff_char_limit)),
            github_token=env.get("DL_GITHUB_TOKEN") or None,
            github_dry_run=_bool(env.get("DL_GITHUB_DRY_RUN"), True),
            gemini_model=(env.get("DL_GEMINI_MODEL") or "").strip() or cls.gemini_model,
            gemini_api_key=env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or None,
            gemini_vertexai=_bool(env.get("DL_GEMINI_VERTEXAI"), False),
            gemini_access_token=env.get("DL_GEMINI_ACCESS_TOKEN") or None,
            llm_record_path=env.get("DL_LLM_RECORD_PATH") or None,
            gcp_project=env.get("GOOGLE_CLOUD_PROJECT") or None,
            gcp_location=env.get("DL_GEMINI_LOCATION") or env.get("GOOGLE_CLOUD_LOCATION") or cls.gcp_location,
            claude_command=env.get("DL_CLAUDE_COMMAND", cls.claude_command),
            fixtures_dir=env.get("DL_FIXTURES_DIR", cls.fixtures_dir),
            project_name=env.get("DL_PROJECT_NAME", cls.project_name),
            project_repo=(env.get("DL_PROJECT_REPO") or "").strip() or None,
            host=env.get("DL_HOST", cls.host),
            port=int(env.get("DL_PORT", cls.port)),
            recorded_mode=_bool(env.get("DL_RECORDED_MODE"), False),
            cors_origins=tuple(o.strip() for o in env.get("DL_CORS_ORIGINS", "").split(",") if o.strip()),
            public_url=(env.get("DL_PUBLIC_URL") or "").strip().rstrip("/") or None,
            sandbox_salt=env.get("DL_SANDBOX_SALT") or None,
            api_base=(env.get("DL_API_BASE") or "").strip().rstrip("/") or None,
            public_mode=_bool(env.get("DL_PUBLIC_MODE"), False),
            # The Firestore ledger implies the Firestore registry and usage counters.
            firestore=_bool(env.get("DL_FIRESTORE"), False) or store == "firestore",
            showcase_rebuild=_bool(env.get("DL_SHOWCASE_REBUILD"), _bool(env.get("DL_PUBLIC_MODE"), False)),
            sandbox_daily_llm_limit=int(env.get("DL_SANDBOX_DAILY_LLM_LIMIT", cls.sandbox_daily_llm_limit)),
            site_daily_llm_limit=int(env.get("DL_SITE_DAILY_LLM_LIMIT", cls.site_daily_llm_limit)),
            sandbox_daily_create_limit=int(env.get("DL_SANDBOX_DAILY_CREATE_LIMIT", cls.sandbox_daily_create_limit)),
        )


def build_llm(settings: Settings) -> LLMClient:
    """Instantiate the configured LLM backend, wrapped in a recorder when ``llm_record_path`` is set."""
    llm = _backend(settings)
    if settings.llm_record_path:
        return RecordingLLM(llm, settings.llm_record_path)
    return llm


def _backend(settings: Settings) -> LLMClient:
    if settings.llm_backend == "fake":
        return FakeReplayLLM(settings.fixtures_dir)
    if settings.llm_backend == "claude_code":
        return ClaudeCodeLLM(command=settings.claude_command, timeout=settings.llm_timeout)
    if settings.llm_backend == "gemini":
        from decision_ledger.adapters.llm.gemini import GeminiLLM

        return GeminiLLM(
            settings.gemini_model,
            api_key=settings.gemini_api_key,
            vertexai=settings.gemini_vertexai,
            project=settings.gcp_project,
            location=settings.gcp_location,
            timeout=settings.llm_timeout,
            access_token=settings.gemini_access_token,
        )
    raise ValueError(f"DL_LLM_BACKEND must be one of {', '.join(BACKENDS)}; got {settings.llm_backend!r}")


def build_publisher(settings: Settings) -> CommentPublisher:
    """Always dry-run: comments live in the product; no GitHub write API is ever called (DL-004)."""
    return DryRunCommentPublisher()


STORES = ("sqlite", "firestore")
AUTH_MODES = ("none", "firebase", "fake")


class InvalidSettings(ValueError):
    """A configuration that must not serve (for example fake sign-in on the public site)."""


def check_settings(settings: Settings, env: Mapping[str, str] | None = None) -> None:
    """Refuse unsafe or incomplete sign-in and storage settings before serving (DL-032).

    Fake sign-in accepts any token, so it runs only locally: never in public mode, never against the cloud
    store, and never on Cloud Run (which always sets ``K_SERVICE``), whatever the other settings say.
    """
    env = os.environ if env is None else env
    if settings.store not in STORES:
        raise InvalidSettings(f"DL_STORE must be one of {', '.join(STORES)}; got {settings.store!r}")
    if settings.auth_mode not in AUTH_MODES:
        raise InvalidSettings(f"DL_AUTH_MODE must be one of {', '.join(AUTH_MODES)}; got {settings.auth_mode!r}")
    if settings.auth_mode == "fake" and (settings.public_mode or settings.store == "firestore" or env.get("K_SERVICE")):
        raise InvalidSettings(
            "DL_AUTH_MODE=fake accepts any token and is for local tests only; "
            "it cannot run with DL_PUBLIC_MODE=1, DL_STORE=firestore or on Cloud Run"
        )
    if settings.auth_mode == "firebase" and not settings.firebase_project_id:
        raise InvalidSettings("DL_AUTH_MODE=firebase needs DL_FIREBASE_PROJECT_ID (the audience of the ID tokens)")


def build_identity(settings: Settings) -> IdentityVerifier | None:
    """The sign-in token verifier for DL_AUTH_MODE; None when sign-in is off."""
    if settings.auth_mode == "firebase":
        from decision_ledger.adapters.firebase_auth import FirebaseIdentityVerifier

        return FirebaseIdentityVerifier(settings.firebase_project_id or "")
    if settings.auth_mode == "fake":
        from decision_ledger.adapters.firebase_auth import FakeIdentityVerifier

        return FakeIdentityVerifier()
    return None


def open_store(settings: Settings) -> LedgerStore:
    """Open the ledger: Firestore with DL_STORE=firestore (survives redeploys), else the SQLite file.

    SQLite raises SchemaVersionMismatch (leaving the file untouched) when it is from another version.
    """
    if settings.store == "firestore":
        from decision_ledger.adapters.firestore_ledger import FirestoreLedgerStore

        return FirestoreLedgerStore(_firestore_client(settings))
    return SqliteLedgerStore(settings.db_path)


def ensure_default_project(store: LedgerStore, name: str, repo: str | None = None) -> Project:
    """Return the first project, creating one named ``name`` (mapped to GitHub ``repo``) when there is none."""
    projects = store.list_projects()
    if projects:
        return store.get_project(projects[0].id) or projects[0]
    return store.create_project(name, repo)


class ContainerFactory:
    """Implements ``ContainerProvider``: one cached Container per project, sharing the LLM and publisher."""

    def __init__(
        self,
        store: LedgerStore,
        llm: LLMClient,
        publisher: CommentPublisher,
        pr_source: PullRequestSource,
        *,
        webhook_secret: str | None = None,
        diff_char_limit: int = 20_000,
        registry: SandboxRegistry | None = None,
        public_url: str | None = None,
        replays: Mapping[int, PullRequestDiff] | None = None,
        replay_requires: tuple[str, ...] = (),
        usage: UsageCounter | None = None,
        sandbox_daily_llm_limit: int = 60,
        site_daily_llm_limit: int = 1500,
        sandbox_daily_create_limit: int = 100,
        identity: IdentityVerifier | None = None,
        auth_required: bool = False,
        daily_new_user_limit: int = 100,
    ):
        self.store = store
        self.identity = identity
        self.llm = llm
        self.publisher = publisher
        self.pr_source = pr_source
        self.webhook_secret = webhook_secret
        self.diff_char_limit = diff_char_limit
        self.public_url = public_url
        self.replays = dict(replays or {})
        self.replay_requires = replay_requires
        self.usage = usage
        self.sandbox_daily_llm_limit = sandbox_daily_llm_limit
        self.site_daily_llm_limit = site_daily_llm_limit
        self.projects = ProjectService(store, auth_required=auth_required)
        self.sandboxes = SandboxService(
            store, registry or InMemorySandboxRegistry(), self.forget, usage=usage, daily_create_limit=sandbox_daily_create_limit,
            auth_required=auth_required, daily_new_user_limit=daily_new_user_limit,
        )
        self._containers: dict[str, Container] = {}

    def for_project(self, project_id: str) -> Container:
        if project_id not in self._containers:
            project = self.store.get_project(project_id)
            if project is None:
                raise NotFound(f"project {project_id} not found")
            self._containers[project_id] = self._build(project)
        return self._containers[project_id]

    def forget(self, project_id: str) -> None:
        """Drop the cached services of a project (after its data was reset)."""
        self._containers.pop(project_id, None)

    def build_with(self, project_id: str, llm: LLMClient) -> Container:
        """Uncached services for one project wired to another LLM (the recorded one, for the showcase rebuild).

        Its calls count toward the site-wide daily limit only, never a sandbox limit (DL-021).
        """
        project = self.store.get_project(project_id)
        if project is None:
            raise NotFound(f"project {project_id} not found")
        return self._build(project, llm)

    def _build(self, project: Project, llm: LLMClient | None = None) -> Container:
        repo = self.store.for_project(project.id)
        override, llm = llm is not None, llm or self.llm
        if self.usage is not None:  # DL-021: every model call of this project counts toward the daily limits
            llm = LimitedLLM(
                llm, self.usage, project_id=project.id,
                sandbox_limit=None if override else self.sandbox_daily_llm_limit, site_limit=self.site_daily_llm_limit,
            )
        return Container(
            ingestion=IngestionService(
                repo, llm, ReconciliationService(llm), AssumptionWatchService(llm), TopicAssigner(llm)
            ),
            deviation=DeviationService(
                repo, llm, self.publisher, self.pr_source, diff_char_limit=self.diff_char_limit, public_url=self.public_url,
                replays=self.replays, replay_requires=self.replay_requires,
            ),
            alerts=AlertService(repo),
            qa=QAService(repo, llm),
            query=LedgerQuery(repo, backend=llm.name, project=project.name),
            topics=TopicService(repo),
        )


def build_provider(settings: Settings) -> tuple[ContainerFactory, Project]:
    """Open the store, make sure a default project exists, and wire the per-project factory."""
    store = open_store(settings)
    default = ensure_default_project(store, settings.project_name, settings.project_repo)
    factory = ContainerFactory(
        store,
        build_llm(settings),
        build_publisher(settings),
        GitHubPullRequestSource(settings.github_token),
        webhook_secret=settings.webhook_secret,
        diff_char_limit=settings.diff_char_limit,
        registry=build_registry(settings),
        public_url=settings.public_url,
        replays=load_demo_pull_requests(),
        replay_requires=load_demo_source_hashes(),
        usage=build_usage_counter(settings, store),
        sandbox_daily_llm_limit=settings.sandbox_daily_llm_limit,
        site_daily_llm_limit=settings.site_daily_llm_limit,
        sandbox_daily_create_limit=settings.sandbox_daily_create_limit,
        identity=build_identity(settings),
        auth_required=settings.auth_mode != "none",
        daily_new_user_limit=settings.daily_new_user_limit,
    )
    factory.sandboxes.showcase_id = default.id
    if settings.public_mode or settings.auth_mode != "none":
        # The default project is the read-only showcase; locally it stays an ordinary writable project.
        factory.projects.showcase_id = default.id
    return factory, default


def _firestore_client(settings: Settings):  # pragma: no cover - needs the cloud extra and credentials
    return _client_for(settings.gcp_project)


@functools.lru_cache(maxsize=4)
def _client_for(project: str | None):  # pragma: no cover - needs the cloud extra and credentials
    """One Firestore client per project, shared by the ledger, the usage counters and the sandbox registry."""
    from google.cloud import firestore

    return firestore.Client(project=project)


def build_registry(settings: Settings) -> SandboxRegistry:
    """Firestore when DL_FIRESTORE is on (needs DL_SANDBOX_SALT), otherwise in-memory."""
    if settings.firestore:
        from decision_ledger.adapters.firestore_sandboxes import FirestoreSandboxRegistry

        return FirestoreSandboxRegistry(_firestore_client(settings), settings.sandbox_salt or "")
    return InMemorySandboxRegistry()


def build_usage_counter(settings: Settings, store: UsageCounter) -> UsageCounter:
    """Firestore when DL_FIRESTORE is on (survives restarts), otherwise the SQLite ledger ``store`` itself."""
    if settings.firestore:
        from decision_ledger.adapters.firestore_usage import FirestoreUsageCounter

        return FirestoreUsageCounter(_firestore_client(settings))
    return store


def _manifest(demo_dir: Path) -> dict:
    return json.loads((demo_dir / "manifest.json").read_text(encoding="utf-8"))


def showcase_complete(store: LedgerStore, project_id: str, demo_dir: Path = DEMO_DIR) -> bool:
    """True when the project holds every scripted source and PR check of the demo scenario."""
    steps = _manifest(demo_dir)["steps"]
    sources = sum(1 for s in steps if s["type"] != "prs")
    prs = sum(len(s["pull_requests"]) for s in steps if s["type"] == "prs")
    repo = store.for_project(project_id)
    return len(repo.list_sources()) >= sources and len(repo.list_pr_checks()) >= prs


_REBUILD_LOCK = threading.Lock()


def rebuild_showcase(
    factory: ContainerFactory, project_id: str, state: ShowcaseState, demo_dir: Path = DEMO_DIR,
    fixtures_dir: Path | None = None,
) -> None:
    """Rebuild the showcase from the recorded scenario unless it is already complete.

    Uses the recorded responses (never the configured LLM), so it costs no LLM calls. A half-built project
    is cleared first. Failures are logged and reported through ``state``; they never stop the server.
    """
    with _REBUILD_LOCK:
        store = factory.store
        if showcase_complete(store, project_id, demo_dir):
            state.status = "ready"
            return
        state.status = "rebuilding"
        try:
            repo = store.for_project(project_id)
            if repo.list_sources() or repo.list_pr_checks():
                store.reset_project(project_id)
            factory.forget(project_id)
            recorded = FakeReplayLLM(str(fixtures_dir or demo_dir / "llm"))
            run_demo(factory.build_with(project_id, recorded), demo_dir)
            state.status = "ready" if showcase_complete(store, project_id, demo_dir) else "failed"
        except Exception:
            log.exception("showcase rebuild failed")
            state.status = "failed"
            try:  # never leave a half-built showcase on screen; the next start rebuilds it
                store.reset_project(project_id)
                factory.forget(project_id)
            except Exception:
                log.exception("could not clear the half-built showcase")


def _demo_steps(demo_dir: Path) -> list[dict]:
    path = demo_dir / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))["steps"] if path.is_file() else []


def _step_pull_requests(demo_dir: Path, step: dict) -> list[PullRequestDiff]:
    return [
        PullRequestDiff(**{k: v for k, v in pr.items() if k != "file"}, diff=(demo_dir / pr["file"]).read_text(encoding="utf-8"))
        for pr in step["pull_requests"]
    ]


def load_demo_pull_requests(demo_dir: Path = DEMO_DIR) -> dict[int, PullRequestDiff]:
    """The demo PRs that can be replayed, keyed by PR number (empty when the demo files are incomplete)."""
    try:
        return {
            pr.pr_number: pr
            for step in _demo_steps(demo_dir) if step["type"] == "prs"
            for pr in _step_pull_requests(demo_dir, step) if pr.pr_number is not None
        }
    except (OSError, KeyError, ValueError):
        log.warning("demo PR fixtures under %s are incomplete; replay is disabled", demo_dir)
        return {}


def load_demo_source_hashes(demo_dir: Path = DEMO_DIR) -> tuple[str, ...]:
    """Content hashes of the demo sources imported before the PR step (what a replay depends on)."""
    hashes: list[str] = []
    try:
        for step in _demo_steps(demo_dir):
            if step["type"] == "prs":
                break
            hashes.append(content_hash((demo_dir / step["file"]).read_text(encoding="utf-8")))
    except (OSError, KeyError, ValueError):
        log.warning("demo source fixtures under %s are incomplete; replay is disabled", demo_dir)
        return ()
    return tuple(hashes)


def run_demo(container: Container, demo_dir: Path = DEMO_DIR) -> dict[str, int]:
    """Import the scripted demo sources and PRs; return decision and alert counts."""
    for step in _demo_steps(demo_dir):
        if step["type"] == "prs":
            for pr in _step_pull_requests(demo_dir, step):
                container.deviation.check(pr)
        else:
            raw = (demo_dir / step["file"]).read_text(encoding="utf-8")
            container.ingestion.ingest(SourceKind(step["kind"]), step["title"], raw, step.get("occurred_on"))
    overview = container.query.overview()
    open_kinds = Counter(a.kind for a in overview.open_alerts if a.status is AlertStatus.OPEN)
    return {
        "decisions": len(overview.decisions),
        **{kind.value: open_kinds.get(kind, 0) for kind in AlertKind},
    }


def build_runtime(
    settings: Settings,
    showcase_project: str | None = None,
    showcase_state: ShowcaseState | None = None,
    showcase_recorded: bool = False,
) -> RuntimeInfo:
    """Describe the wiring for ``/healthz``; ``recorded`` is on with DL_RECORDED_MODE or the Fake backend."""
    from decision_ledger import __version__

    model = settings.gemini_model if settings.llm_backend == "gemini" else None
    return RuntimeInfo(
        version=__version__,
        llm_backend=settings.llm_backend,
        model=model,
        store=settings.store,
        public_mode=settings.public_mode,
        recorded=settings.recorded_mode or settings.llm_backend == "fake",
        showcase_project=showcase_project,
        showcase_recorded=showcase_recorded,
        showcase_state=showcase_state or ShowcaseState(),
        api_base=settings.api_base,
        auth_mode=settings.auth_mode,
        firebase=(
            {"apiKey": settings.firebase_api_key, "authDomain": settings.firebase_auth_domain, "projectId": settings.firebase_project_id}
            if settings.auth_mode == "firebase"
            else None
        ),
    )


def serve(settings: Settings, host: str | None = None, port: int | None = None) -> None:
    """Run the API and web UI; refuses (``ModelNotAllowed``) a Gemini model that retires too early."""
    from decision_ledger.api.app import create_app

    check_settings(settings)
    if settings.llm_backend == "gemini":
        check_gemini_model(settings.gemini_model)

    provider, default = build_provider(settings)
    state = ShowcaseState()
    if settings.showcase_rebuild:
        rebuild_showcase(provider, default.id, state)
    runtime = build_runtime(
        settings, showcase_project=default.id, showcase_state=state, showcase_recorded=settings.showcase_rebuild
    )
    uvicorn.run(create_app(provider, runtime, settings.cors_origins), host=host or settings.host, port=port or settings.port)
