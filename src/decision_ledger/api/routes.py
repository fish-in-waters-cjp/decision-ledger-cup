"""HTTP routes. Each handler parses input and delegates to one service — no business logic here."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from decision_ledger.api.errors import ProblemError
from decision_ledger.api.webhook_auth import verify_signature
from decision_ledger.domain.errors import ParseError, Unauthenticated
from decision_ledger.domain.models import (
    Alert,
    AlertStatus,
    Decision,
    ImportReport,
    Principal,
    PrCheck,
    Project,
    ProjectSummary,
    PullRequestDiff,
    PullRequestEvent,
    Resolution,
    Source,
    SourceKind,
    Topic,
)
from decision_ledger.services.container import Container, ContainerProvider
from decision_ledger.services.deviation import DeviationResult, DeviationService, idempotency_key
from decision_ledger.services.qa import Answer, DecisionView, Overview, TopicCard, TopicDetail

router = APIRouter()
log = logging.getLogger(__name__)

PR_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
# GitHub gives up on a webhook after 10 s; resolving the diff must finish well before that.
WEBHOOK_DIFF_DEADLINE = 8.0


class DeviationResponse(BaseModel):
    check: PrCheck
    alerts: list[Alert]
    reused: bool


class PrCheckRequest(BaseModel):
    """A PR to check: a unified ``diff``, or ``fixture_pr`` to replay a built-in demo PR (exactly one)."""

    repo: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    title: str = ""
    author: str = ""
    diff: str | None = None
    fixture_pr: Literal[87, 88] | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> PrCheckRequest:
        if (self.diff is None) == (self.fixture_pr is None):
            raise ValueError("send exactly one of diff or fixture_pr")
        return self


class ReaffirmRequest(BaseModel):
    note: str = ""


class AskRequest(BaseModel):
    question: str = Field(min_length=1)


class ProjectRequest(BaseModel):
    name: str
    repo: str | None = None


class ProjectUpdate(BaseModel):
    repo: str | None = None


class MeResponse(BaseModel):
    uid: str
    project: Project


class RenameRequest(BaseModel):
    name: str


class MergeRequest(BaseModel):
    into: str


class MoveRequest(BaseModel):
    decision_ids: list[str] = Field(min_length=1)
    into: str


class SplitRequest(BaseModel):
    decision_ids: list[str] = Field(min_length=1)
    name: str


def _provider(request: Request) -> ContainerProvider:
    return request.app.state.provider


def _principal(request: Request) -> Principal | None:
    """The verified identity of the request (DL-032); None without a token or when sign-in is off.

    A token that is present but invalid is a 401 rather than "anonymous", so an expired session is noticed.
    """
    identity = _provider(request).identity
    header = request.headers.get("authorization", "")
    if identity is None or not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated("use an Authorization: Bearer <Firebase ID token> header")
    return identity.verify(token.strip())


def _signed_in(request: Request) -> Principal:
    principal = _principal(request)
    if principal is None:
        raise Unauthenticated("Sign in with Google to continue")
    return principal


def _container(request: Request, project_id: str, *, write: bool = False) -> Container:
    """The services of one project after the access check (unknown → 404, no token → 401, not yours → 403).

    Reading the showcase never needs a token, so an expired one does not turn that read into a 401 (REQ-PROJECT-006).
    """
    provider = _provider(request)
    try:
        principal = _principal(request)
    except Unauthenticated:
        if write or project_id != provider.projects.showcase_id:
            raise
        principal = None
    provider.projects.authorize(project_id, principal, write=write)
    return provider.for_project(project_id)


P = "/api/projects/{project_id}"


@router.get("/api/projects", response_model=list[ProjectSummary])
def list_projects(request: Request) -> list[ProjectSummary]:
    provider = _provider(request)
    runtime = getattr(request.app.state, "runtime", None)
    if (runtime is not None and runtime.public_mode) or provider.projects.auth_required:
        # Other people's sandboxes and workspaces never appear (nor are they read): the showcase, plus my workspace.
        try:
            principal = _principal(request)
        except Unauthenticated:  # like reading the showcase: an expired token still lists it (review R2-02)
            principal = None
        return provider.projects.visible(principal, runtime.showcase_project if runtime else None)
    return provider.projects.list()


@router.get("/api/me", response_model=MeResponse)
def me(request: Request) -> MeResponse:
    principal = _signed_in(request)
    return MeResponse(uid=principal.uid, project=_provider(request).sandboxes.for_user(principal.uid))


@router.post("/api/me/reset", status_code=204)
def reset_me(request: Request) -> Response:
    _provider(request).sandboxes.reset_for_user(_signed_in(request).uid)
    return Response(status_code=204)


@router.get("/api/runtime")
def runtime_config(request: Request) -> dict[str, object]:
    runtime = getattr(request.app.state, "runtime", None)
    return runtime.public_config() if runtime is not None else {"auth_mode": "none"}


@router.patch("/api/projects/{project_id}", response_model=Project)
def update_project(request: Request, project_id: str, body: ProjectUpdate) -> Project:
    provider = _provider(request)
    provider.projects.authorize(project_id, _principal(request), write=True)
    return provider.projects.set_repo(project_id, body.repo)


class SandboxResponse(BaseModel):
    id: str
    name: str
    url: str


def _sandbox(project: Project) -> SandboxResponse:
    return SandboxResponse(id=project.id, name=project.name, url=f"/s/{project.id}/")


@router.post("/api/sandboxes", response_model=SandboxResponse, status_code=201)
def create_sandbox(request: Request) -> SandboxResponse:
    return _sandbox(_provider(request).sandboxes.create())


@router.get("/api/sandboxes/{sandbox_id}", response_model=SandboxResponse)
def get_sandbox(request: Request, sandbox_id: str) -> SandboxResponse:
    return _sandbox(_provider(request).sandboxes.get(sandbox_id))


@router.post("/api/sandboxes/{sandbox_id}/reset", status_code=204)
def reset_sandbox(request: Request, sandbox_id: str) -> Response:
    _provider(request).sandboxes.reset(sandbox_id)
    return Response(status_code=204)


@router.post("/api/projects", response_model=Project, status_code=201)
def create_project(request: Request, body: ProjectRequest) -> Project:
    return _provider(request).projects.create(body.name, body.repo)


def _deviation(result: DeviationResult) -> DeviationResponse:
    return DeviationResponse(check=result.check, alerts=result.alerts, reused=result.reused)


@router.post(P + "/sources", response_model=ImportReport)
def import_source(
    request: Request,
    project_id: str,
    kind: str = Form(...),
    file: UploadFile = File(...),
    title: str = Form(""),
    occurred_on: str | None = Form(None),
) -> ImportReport:
    container = _container(request, project_id, write=True)
    try:
        source_kind = SourceKind(kind)
    except ValueError as exc:
        raise ParseError(f"unknown source kind: {kind}") from exc
    try:
        raw_text = file.file.read().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError("source file must be UTF-8 text") from exc
    return container.ingestion.ingest(source_kind, title or file.filename or "untitled", raw_text, occurred_on or None)


@router.post("/webhook/github", response_model=None)
@router.post("/webhooks/github", response_model=None)
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    x_github_event: str = Header(""),
    x_hub_signature_256: str | None = Header(None),
):
    body = await request.body()
    provider = _provider(request)
    # Verify first: nothing about the payload (not even its repo) is trusted before the signature.
    if not verify_signature(provider.webhook_secret, body, x_hub_signature_256):
        raise ProblemError(401, "invalid-signature", "Webhook signature verification failed")
    if x_github_event == "ping":
        return {"ok": True}
    if x_github_event != "pull_request":
        return JSONResponse({"ignored": x_github_event}, status_code=202)
    event = _pull_request_event(body)
    if event.action not in PR_ACTIONS:
        return JSONResponse({"ignored": f"pull_request.{event.action}"}, status_code=202)
    projects = provider.projects.by_repo(event.repo)
    if not projects:
        return JSONResponse({"unmapped": event.repo}, status_code=202)
    # DL-032: every project registering the repo gets the PR; the diff is resolved once (they share the source).
    deviations = [provider.for_project(p.id).deviation for p in projects]
    # GitHub expects a 2XX within 10 s: resolve the diff now (a failure is still a 422), check after responding.
    try:
        pr = await asyncio.wait_for(run_in_threadpool(deviations[0].resolve_webhook, event), WEBHOOK_DIFF_DEADLINE)
    except TimeoutError as exc:
        raise ProblemError(422, "diff-unavailable", "PR diff unavailable", f"diff download exceeded {WEBHOOK_DIFF_DEADLINE:g} s") from exc
    for deviation in deviations:
        background.add_task(_check_in_background, deviation, pr)
    return JSONResponse({"queued": idempotency_key(pr), "projects": len(deviations)}, status_code=202)


def _check_in_background(deviation: DeviationService, pr: PullRequestDiff) -> None:
    """Run the deviation check after the response; failures are logged with the key, never retried."""
    try:
        deviation.check(pr)
    except Exception:
        log.exception("background deviation check failed for %s", idempotency_key(pr))


def _pull_request_event(body: bytes) -> PullRequestEvent:
    try:
        payload = json.loads(body)
        pr = payload["pull_request"]
        return PullRequestEvent(
            action=payload.get("action", ""),
            repo=payload["repository"]["full_name"],
            pr_number=payload.get("number", pr.get("number")),
            head_sha=pr["head"]["sha"],
            title=pr.get("title", ""),
            author=(pr.get("user") or {}).get("login", ""),
            diff_url=pr.get("diff_url"),
            diff=payload.get("diff"),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
        raise ProblemError(400, "invalid-payload", "Malformed webhook payload", str(exc)) from exc


@router.post(P + "/pr-check", response_model=DeviationResponse)
def pr_check(request: Request, project_id: str, body: PrCheckRequest) -> DeviationResponse:
    deviation = _container(request, project_id, write=True).deviation
    if body.fixture_pr is not None:
        return _deviation(deviation.replay(body.fixture_pr))
    return _deviation(deviation.check(PullRequestDiff(**body.model_dump(exclude={"fixture_pr"}))))


@router.post(P + "/alerts/{alert_id}/resolve", response_model=Alert)
def resolve_alert(request: Request, project_id: str, alert_id: str, body: Resolution) -> Alert:
    return _container(request, project_id, write=True).alerts.resolve(alert_id, body)


@router.post(P + "/decisions/{decision_id}/reaffirm", response_model=Decision)
def reaffirm_decision(request: Request, project_id: str, decision_id: str, body: ReaffirmRequest) -> Decision:
    return _container(request, project_id, write=True).alerts.reaffirm(decision_id, body.note)


@router.post(P + "/ask", response_model=Answer)
def ask(request: Request, project_id: str, body: AskRequest) -> Answer:
    return _container(request, project_id, write=True).qa.answer(body.question)


@router.get(P + "/overview", response_model=Overview)
def overview(request: Request, project_id: str) -> Overview:
    return _container(request, project_id).query.overview()


@router.get(P + "/decisions", response_model=list[DecisionView])
def list_decisions(request: Request, project_id: str) -> list[DecisionView]:
    return _container(request, project_id).query.list()


@router.get(P + "/topics", response_model=list[TopicCard])
def list_topics(request: Request, project_id: str) -> list[TopicCard]:
    return _container(request, project_id).query.topics()


@router.get(P + "/topics/{topic_id}", response_model=TopicDetail)
def topic_detail(request: Request, project_id: str, topic_id: str) -> TopicDetail:
    return _container(request, project_id).query.topic_detail(topic_id)


@router.post(P + "/topics/{topic_id}/rename", response_model=Topic)
def rename_topic(request: Request, project_id: str, topic_id: str, body: RenameRequest) -> Topic:
    return _container(request, project_id, write=True).topics.rename(topic_id, body.name)


@router.post(P + "/topics/{topic_id}/merge", response_model=Topic)
def merge_topic(request: Request, project_id: str, topic_id: str, body: MergeRequest) -> Topic:
    return _container(request, project_id, write=True).topics.merge(topic_id, body.into)


@router.post(P + "/topics/{topic_id}/move", response_model=Topic)
def move_decisions(request: Request, project_id: str, topic_id: str, body: MoveRequest) -> Topic:
    return _container(request, project_id, write=True).topics.move(topic_id, body.decision_ids, body.into)


@router.post(P + "/topics/{topic_id}/split", response_model=Topic)
def split_topic(request: Request, project_id: str, topic_id: str, body: SplitRequest) -> Topic:
    return _container(request, project_id, write=True).topics.split(topic_id, body.decision_ids, body.name)


@router.get(P + "/alerts", response_model=list[Alert])
def list_alerts(request: Request, project_id: str, status: AlertStatus | None = None) -> list[Alert]:
    return _container(request, project_id).query.alerts(status)


@router.get(P + "/pr-checks", response_model=list[PrCheck])
def list_pr_checks(request: Request, project_id: str) -> list[PrCheck]:
    return _container(request, project_id).query.pr_checks()


@router.get(P + "/sources/{source_id}", response_model=Source)
def get_source(request: Request, project_id: str, source_id: str) -> Source:
    return _container(request, project_id).query.source(source_id)
