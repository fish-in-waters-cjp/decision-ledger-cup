"""The services the API layer talks to: one ``Container`` per project, reached through a ``ContainerProvider``.

``bootstrap`` implements the provider; the API depends only on this protocol, never on bootstrap.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Protocol

from decision_ledger.domain.ports import IdentityVerifier
from decision_ledger.services.alerts import AlertService
from decision_ledger.services.deviation import DeviationService
from decision_ledger.services.ingestion import IngestionService
from decision_ledger.services.projects import ProjectService
from decision_ledger.services.qa import LedgerQuery, QAService
from decision_ledger.services.sandboxes import SandboxService
from decision_ledger.services.topics import TopicService


@dataclass
class Container:
    """Services bound to one project's repository."""

    ingestion: IngestionService
    deviation: DeviationService
    alerts: AlertService
    qa: QAService
    query: LedgerQuery
    topics: TopicService


class ShowcaseState:
    """Mutable status of the showcase project: absent, rebuilding, ready or failed."""

    def __init__(self, status: str = "absent"):
        self.status = status


@dataclass(frozen=True)
class RuntimeInfo:
    """What the running instance is wired to; reported by the health endpoint, never used for business logic."""

    version: str = "0.0.0"
    llm_backend: str = "unknown"
    model: str | None = None
    store: str = "sqlite"
    public_mode: bool = False
    recorded: bool = False
    showcase_project: str | None = None
    showcase_recorded: bool = False
    # Origin for the long LLM requests (imports, questions, PR checks); None keeps them same-origin.
    api_base: str | None = None
    showcase_state: ShowcaseState = field(default_factory=ShowcaseState, compare=False)
    # Random per process start: tells whether an expired sandbox was lost to an instance replacement.
    instance_id: str = field(default_factory=lambda: secrets.token_hex(8))
    # DL-032: how users sign in ("none", "firebase" or "fake") and the Firebase web config the UI needs.
    auth_mode: str = "none"
    firebase: dict | None = field(default=None, compare=False)

    def public_config(self) -> dict[str, object]:
        """The ``/api/runtime`` body: what the web UI needs to start (sign-in setup, showcase, API origin)."""
        return {
            "auth_mode": self.auth_mode,
            "firebase": self.firebase,
            "public_mode": self.public_mode,
            "showcase_project": self.showcase_project,
            "api_base": self.api_base,
        }

    def health(self) -> dict[str, object]:
        """The ``/healthz`` body."""
        return {
            "status": "ok",
            "version": self.version,
            "llm_backend": self.llm_backend,
            "model": self.model,
            "store": self.store,
            "public_mode": self.public_mode,
            "recorded": self.recorded,
            "showcase": self.showcase_state.status,
            "showcase_project": self.showcase_project,
            "showcase_recorded": self.showcase_recorded,
            "instance_id": self.instance_id,
            "api_base": self.api_base,
        }


class ContainerProvider(Protocol):
    projects: ProjectService
    sandboxes: SandboxService
    # Global: a webhook's signature is verified before its repo is mapped to a project.
    webhook_secret: str | None
    # DL-032: verifies sign-in tokens; None when sign-in is off (local runs).
    identity: IdentityVerifier | None

    def for_project(self, project_id: str) -> Container:
        """The project's services; raises NotFound for an unknown project."""
        ...

    def forget(self, project_id: str) -> None:
        """Drop cached services after the project's data was reset."""
        ...
