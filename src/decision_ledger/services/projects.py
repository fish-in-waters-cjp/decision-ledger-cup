"""Projects: create, list and look up the products a ledger tracks, and decide who may use each (DL-032)."""

from __future__ import annotations

import re

from decision_ledger.domain.errors import Forbidden, InvalidProjectOperation, NotFound, Unauthenticated
from decision_ledger.domain.models import Principal, Project, ProjectSummary
from decision_ledger.domain.ports import LedgerStore

# GitHub's owner/name: letters, digits, "-", "_" and "." on each side of exactly one slash; GitHub caps an owner at
# 39 characters and a repository name at 100, which also keeps a project record far below Firestore's limit.
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
MAX_NAME = 200
SHOWCASE_READ_ONLY = "The showcase project is read-only. Sign in with Google to get your own workspace."


class ProjectService:
    def __init__(self, store: LedgerStore, *, showcase_id: str | None = None, auth_required: bool = False):
        self.store = store
        # The read-only public project; anyone may read it, nobody may write to it.
        self.showcase_id = showcase_id
        # True when sign-in is on (DL_AUTH_MODE firebase or fake): every other project belongs to its owner.
        self.auth_required = auth_required

    def create(self, name: str, repo: str | None = None) -> Project:
        """Create a project; the name must be non-blank and unique. Signed-in users get workspaces instead."""
        if self.auth_required:
            raise Forbidden("Each account has one workspace; sign in with Google to get yours.")
        name, repo = name.strip(), (repo or "").strip() or None
        if not name:
            raise InvalidProjectOperation("Project name cannot be blank")
        if len(name) > MAX_NAME:
            raise InvalidProjectOperation(f"Project name is longer than {MAX_NAME} characters")
        if any(p.name == name for p in self.store.list_projects()):
            raise InvalidProjectOperation(f"Project name \"{name}\" is already taken")
        return self.store.create_project(name, repo)

    def list(self) -> list[ProjectSummary]:
        """All projects with their open-alert counts, oldest first."""
        return self.store.list_projects()

    def visible(self, principal: Principal | None, showcase_id: str | None = None) -> list[ProjectSummary]:
        """What a public or signed-in page lists: the showcase, plus the caller's own workspace. Reads only those."""
        mine = self.store.project_by_owner(principal.uid) if principal else None
        showcase = self.showcase_id or showcase_id
        return self.store.list_projects([i for i in (showcase, mine.id if mine else None) if i])

    def get(self, project_id: str) -> Project:
        project = self.store.get_project(project_id)
        if project is None:
            raise NotFound(f"project {project_id} not found")
        return project

    def authorize(self, project_id: str, principal: Principal | None, *, write: bool) -> Project:
        """The project if ``principal`` may read it (or write to it); otherwise NotFound, Unauthenticated or Forbidden."""
        project = self.get(project_id)
        if project.id == self.showcase_id:
            if write:
                raise Forbidden(SHOWCASE_READ_ONLY)
            return project
        if not self.auth_required:
            # A signed-in user's workspace stays closed if sign-in is ever switched off (defense in depth, DL-032).
            if project.owner_uid is not None:
                raise Forbidden("This workspace belongs to a signed-in account; sign-in is not enabled on this server")
            return project
        if principal is None:
            raise Unauthenticated("Sign in with Google to use this project")
        if project.owner_uid != principal.uid:
            raise Forbidden("This project belongs to another account")
        return project

    def set_repo(self, project_id: str, repo: str | None) -> Project:
        """Register (or clear, with an empty value) the GitHub repo whose PRs this project checks."""
        repo = (repo or "").strip() or None
        if repo is not None and not _REPO.match(repo):
            raise InvalidProjectOperation("Use the owner/name format for the GitHub repository")
        project = self.get(project_id).model_copy(update={"repo": repo})
        self.store.update_project(project)
        return project

    def by_repo(self, repo: str) -> list[Project]:
        """Every project registering a GitHub repo (several workspaces may follow one repo), oldest first."""
        return self.store.projects_by_repo(repo)
