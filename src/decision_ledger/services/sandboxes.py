"""Judge sandboxes (DL-018): each sandbox is a project whose id is an unguessable random token.

Holding the id is what grants access, so ids come from ``secrets`` and are never sequential.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable

from decision_ledger.domain.errors import (
    Forbidden,
    InvalidProjectOperation,
    NotFound,
    SandboxExpired,
    SandboxLimitExceeded,
    UserLimitExceeded,
)
from decision_ledger.domain.models import Project
from decision_ledger.domain.ports import LedgerStore, SandboxRegistry, UsageCounter
from decision_ledger.services.usage import usage_day

EXPIRED = "Sandbox expired, start a new one"
# Counter scope for sandbox creations per UTC day. Each sandbox can spend LLM calls, so anonymous
# creation is capped to keep the site-wide daily LLM budget reachable for everyone.
CREATE_SCOPE = "site-sandboxes"
# Counter scope for new signed-in workspaces per UTC day (DL-032): replaces the sandbox limit when sign-in is on.
USER_SCOPE = "site-users"
SIGN_IN_INSTEAD = "Sign in with Google to get your own workspace."
_ATTEMPTS = 5


def new_sandbox_id() -> str:
    """16 url-safe characters (96 random bits)."""
    return secrets.token_urlsafe(12)


def workspace_id(uid: str) -> str:
    """A signed-in user's workspace id: "w" + 15 hex chars of sha256(uid). Stable, so two tabs signing in at
    once collide on the same id instead of creating two workspaces; it reveals nothing about the uid."""
    return "w" + hashlib.sha256(uid.encode("utf-8")).hexdigest()[:15]


class SandboxService:
    def __init__(
        self,
        store: LedgerStore,
        registry: SandboxRegistry,
        forget: Callable[[str], None],
        *,
        showcase_id: str | None = None,
        new_id: Callable[[], str] = new_sandbox_id,
        usage: UsageCounter | None = None,
        daily_create_limit: int = 100,
        today: Callable[[], str] = usage_day,
        auth_required: bool = False,
        daily_new_user_limit: int = 100,
    ):
        self.store, self.registry, self._forget = store, registry, forget
        self.showcase_id = showcase_id
        self._new_id = new_id
        self.usage, self.daily_create_limit, self._today = usage, daily_create_limit, today
        # Sign-in on (DL-032): anonymous sandboxes are off; each user gets one workspace keyed by uid.
        self.auth_required, self.daily_new_user_limit = auth_required, daily_new_user_limit

    def create(self) -> Project:
        """A new empty sandbox project; a colliding id is redrawn. Refused once today's creation limit is passed."""
        if self.auth_required:
            raise Forbidden(SIGN_IN_INSTEAD)
        if self.usage is not None and self.usage.increment(CREATE_SCOPE, self._today()) > self.daily_create_limit:
            raise SandboxLimitExceeded(f"daily limit of {self.daily_create_limit} new sandboxes reached")
        for _ in range(_ATTEMPTS):
            sandbox_id = self._new_id()
            if self.store.get_project(sandbox_id) is None and not self.registry.exists(sandbox_id):
                project = self.store.create_project(f"Sandbox {sandbox_id}", None, project_id=sandbox_id)
                self.registry.register(sandbox_id)
                return project
        raise InvalidProjectOperation("could not allocate a sandbox id")

    def get(self, sandbox_id: str) -> Project:
        """The live sandbox; ``SandboxExpired`` when it was handed out but its ledger is gone, else ``NotFound``."""
        if self.auth_required:
            raise Forbidden(SIGN_IN_INSTEAD)
        registered = sandbox_id != self.showcase_id and self.registry.exists(sandbox_id)
        project = self.store.get_project(sandbox_id) if registered else None
        if project is not None:
            return project
        if registered:
            raise SandboxExpired(EXPIRED)
        raise NotFound(EXPIRED)

    def reset(self, sandbox_id: str) -> None:
        """Delete everything in this sandbox only; the showcase can never be reset."""
        self.get(sandbox_id)  # the showcase is never a registered sandbox, so it is NotFound here
        if sandbox_id == self.showcase_id:
            raise InvalidProjectOperation("the showcase project cannot be reset")
        self.store.reset_project(sandbox_id)
        self._forget(sandbox_id)

    # --- signed-in workspaces (DL-032) ---------------------------------------

    def for_user(self, uid: str) -> Project:
        """The user's workspace, created on first sign-in (refused once today's new-user limit is passed)."""
        existing = self.store.project_by_owner(uid)
        if existing is not None:
            return existing
        if self.usage is not None and self.usage.increment(USER_SCOPE, self._today()) > self.daily_new_user_limit:
            raise UserLimitExceeded(f"daily limit of {self.daily_new_user_limit} new accounts reached; try again tomorrow")
        project_id = workspace_id(uid)
        try:
            return self.store.create_project(f"Workspace {project_id}", None, project_id=project_id, owner_uid=uid)
        except InvalidProjectOperation:
            # Another request of the same user created it first (same stable id): use that one.
            project = self.store.project_by_owner(uid)
            if project is None:
                raise
            return project

    def reset_for_user(self, uid: str) -> None:
        """Delete everything in the user's workspace; the workspace and its owner stay."""
        project = self.store.project_by_owner(uid)
        if project is None:
            raise NotFound("no workspace yet; sign in first")
        self.store.reset_project(project.id)
        self._forget(project.id)
