# Decision Ledger

Decision Ledger reads meeting transcripts and Slack exports, records the decisions a team made with their reasons and the exact source quotes, and keeps checking three things:

- **Reconciliation**: is a new decision the same as an existing one, an update, or a contradiction?
- **Premise watch**: when new information overturns a premise behind a decision, ask for a review.
- **Deviation check**: when a pull request breaks a checkable architecture decision, raise an alert and draft a PR comment (advisory only; it never blocks a merge).

Every AI judgment only raises an alert; a decision changes status only after a person resolves the alert. Decisions and alerts without a source quote are never stored.

## Synthetic data and what the numbers mean

The demo team (the Atlas team building "TrailCart"), its people, meetings, Slack messages and pull requests are invented. The demo replays responses recorded from Gemini (`gemini-3.5-flash`, Vertex AI, 2026-10-17); see `src/decision_ledger/demo/fixtures/llm/RECORDING.md`.

Synthetic fixtures; the gold answers were labelled by the AI team on the owner's behalf. Results describe this test set only; they do not establish production accuracy. Evaluation results and two real failure cases are in [`docs/eval-results.md`](docs/eval-results.md).

## Run locally (no credentials)

Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                          # install dependencies
uv run decision-ledger demo      # replay the demo scenario with the Fake LLM and print counts
uv run decision-ledger serve     # API and web UI at http://127.0.0.1:8000
uv run pytest                    # tests (network disabled)
uv run python scripts/eval.py    # score the demo run against demo/eval/dev-gold.json
```

"Import sources" on the Overview page steps through the five-step demo (three meetings, one Slack export, two pull requests). You can also upload your own transcript (`[hh:mm:ss] Speaker: text`) or a Slack export JSON. Data lives in `DL_DB_PATH` (default `./decision-ledger.db`); to start the demo over, use another `DL_DB_PATH` or delete the file and restart.

## Projects and topics

- **Projects**: an organisation can have several products, one project each. Sources, topics, decisions, alerts, PR checks and questions belong to a project, and reconciliation, premise watch and deviation checks only use data from the same project. Switch or create projects from the project menu; a project can map to a GitHub repo (`owner/name`). Webhooks are routed by repo; an unmapped repo gets 202 `{"unmapped": ...}` and no LLM call. The first start creates a default project named by `DL_PROJECT_NAME` and mapped to `DL_PROJECT_REPO`. Ids are numbered per project, so the demo produces the same result in any project that holds only demo data, including a new empty one; once other data is imported the demo is hidden.
- **Topics**: decisions are grouped by topic. A decision joins an existing topic with the same normalised name; otherwise the LLM decides whether a differently worded topic is the same subject, and a new topic is created when it cannot tell. Topics can be renamed and merged, and decisions can be moved or split into a new topic; none of this changes decision status or raises alerts. Topics without decisions are not shown on the home page or in the topic list.
- **Database schema**: version 3, no automatic migration. If `DL_DB_PATH` holds an older schema, `serve` and `demo` print the path and exit with code 2 without touching the file; delete it, restart and import again.

## LLM backends

The backend is chosen by environment variables only.

| `DL_LLM_BACKEND` | What it does | Needs |
|---|---|---|
| `fake` (default) | Replays recorded responses from `src/decision_ledger/demo/fixtures/llm/` by request key; anything outside the demo returns an empty result | nothing |
| `claude_code` | Calls the local `claude -p` with all tools disabled, in an isolated directory, without `DL_*` variables | a logged-in Claude Code CLI |
| `gemini` | google-genai with JSON-schema output | `uv sync --extra gemini`, plus an API key or Vertex AI settings |

`DL_LLM_RECORD_PATH` wraps any backend in a recorder that saves responses in the Fake format. `scripts/record_demo.py` re-records the demo with a shared 120-call budget (`src/decision_ledger/demo/fixtures/llm/record-calls.txt`).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DL_DB_PATH` | `decision-ledger.db` | SQLite file (`:memory:` for an in-memory database) |
| `DL_PROJECT_NAME` | `Local Ledger` | name of the default project created on first start |
| `DL_PROJECT_REPO` | unset | GitHub repo (`owner/name`) of the default project; webhooks are routed by it; applied only when the project is created |
| `DL_HOST` / `DL_PORT` | `127.0.0.1` / `8000` | `serve` bind address |
| `DL_LLM_TIMEOUT` | `60` | per-call LLM timeout in seconds |
| `DL_LLM_RECORD_PATH` | unset | record every LLM response to this JSON file |
| `DL_FIXTURES_DIR` | `src/decision_ledger/demo/fixtures/llm` | where the Fake backend reads recorded responses |
| `DL_WEBHOOK_SECRET` | unset | GitHub webhook HMAC secret; without it every webhook gets 401 |
| `DL_GITHUB_TOKEN` | unset | used only to download PR diffs read-only; never used to write to GitHub |
| `DL_GITHUB_DRY_RUN` | `true` | retired: PR comments are always shown in the product as a dry-run card; no GitHub write API is called and commit statuses are never set |
| `DL_STORE` | `sqlite` | `firestore` keeps the ledger in Firestore (rooted at `projects/{pid}`), so data survives redeploys; it also puts usage counters and the sandbox registry in Firestore |
| `DL_AUTH_MODE` | `none` | `firebase`: verify Firebase ID tokens (Google sign-in) and isolate workspaces by uid; `fake`: tests only (`fake:<uid>`), refused together with `DL_PUBLIC_MODE=1` |
| `DL_FIREBASE_PROJECT_ID` | unset | Firebase project id (the ID token audience); required with `DL_AUTH_MODE=firebase` |
| `DL_FIREBASE_API_KEY` / `DL_FIREBASE_AUTH_DOMAIN` | unset / `<project id>.firebaseapp.com` | Web config for the Firebase SDK in the browser, served by `GET /api/runtime`, never hard-coded |
| `DL_DAILY_NEW_USER_LIMIT` | `100` | New signed-in workspaces per UTC day; past it `GET /api/me` returns 429 |
| `DL_PUBLIC_URL` | unset | public URL of the product; when set, each violated decision in a stored PR comment links to `{DL_PUBLIC_URL}/#p={project}&d={decision}` |
| `DL_DIFF_CHAR_LIMIT` | `20000` | character limit of the relevant diff sent to the LLM; longer diffs are truncated and the comment says so |
| `DL_SANDBOX_DAILY_LLM_LIMIT` | `60` | AI model calls per project (a signed-in workspace, or an anonymous sandbox when sign-in is off) per UTC day; over the limit the request gets 429 `rate-limited` and the model is not called |
| `DL_SITE_DAILY_LLM_LIMIT` | `1500` | AI model calls per UTC day across the site; over the limit any request gets 429. The showcase rebuild replays recorded responses and never calls the model (a missing recording yields an empty result, not a live call), but its replayed calls, about 13 per rebuild, are counted here |
| `DL_SANDBOX_DAILY_CREATE_LIMIT` | `100` | new anonymous sandboxes per UTC day when sign-in is off; over the limit 429 `sandbox-limit` (with sign-in, `DL_DAILY_NEW_USER_LIMIT` caps new accounts instead) |
| `DL_PUBLIC_MODE` | `false` | public demo: the showcase project is read-only (writes get 403) and projects are created only as each signed-in user's workspace, or as anonymous sandboxes under `/s/<id>/` when sign-in is off |
| `DL_SHOWCASE_REBUILD` | follows `DL_PUBLIC_MODE` | rebuild the showcase from the recorded scenario on start when it is incomplete |
| `DL_RECORDED_MODE` | `false` | show the "Recorded run" banner (always on with the Fake backend) |
| `DL_FIRESTORE` | `false` | keep the sandbox registry and usage counters in Firestore instead of memory and SQLite |
| `DL_SANDBOX_SALT` | unset | HMAC salt for sandbox ids stored in Firestore |
| `DL_CORS_ORIGINS` | unset | comma-separated origins allowed to call the API cross-origin (the Hosting URL) |
| `DL_API_BASE` | unset | origin for long model requests (imports, questions, PR checks), e.g. the Cloud Run URL, so they avoid the Hosting time limit; unset keeps them same-origin |
| `DL_GEMINI_MODEL` | `gemini-3.5-flash` | Gemini model; must be on the allow-list (3.5, 3.7, 3.8-flash) and retire after 2026-12-04, otherwise `serve` exits with code 2 |
| `DL_GEMINI_LOCATION` | `global` | Vertex AI location; falls back to `GOOGLE_CLOUD_LOCATION`, then `global` |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | unset | Gemini Developer API key |
| `DL_GEMINI_VERTEXAI` | `false` | `true` uses Vertex AI (with `GOOGLE_CLOUD_PROJECT`; location from `DL_GEMINI_LOCATION`) |
| `DL_GEMINI_ACCESS_TOKEN` | unset | OAuth access token for Vertex AI instead of Application Default Credentials |
| `DL_CLAUDE_COMMAND` | `claude` | Claude Code CLI path |

## GitHub webhook

Point a repo webhook at `/webhooks/github` (the old `/webhook/github` still works) with content type `application/json`, the same secret as `DL_WEBHOOK_SECRET`, and the Pull requests event. The server verifies `X-Hub-Signature-256` before reading the payload. The diff comes from the payload or a read-only download with `DL_GITHUB_TOKEN` (8 s total; unavailable or late gives 422). Once the diff is in hand the server answers 202 `{"queued": "owner/repo#pr@sha"}` and runs the deviation check in the background; a redelivered revision does not produce a second result. On Cloud Run use `--no-cpu-throttling`, or background work may stall after the response. Webhooks are routed to a project by repo: the default project uses `DL_PROJECT_REPO`, other projects the repo given when they were created. Without GitHub, `POST /api/projects/{project_id}/pr-check` accepts a unified diff, or `{"fixture_pr": 87}` / `88` to replay a built-in demo PR (409 until the scenario's sources are imported; the Pull requests page has a Replay button).

## Google Cloud

Locally, the Google Cloud path is Gemini on Vertex AI:

```bash
uv sync --extra gemini
gcloud auth application-default login
DL_LLM_BACKEND=gemini DL_GEMINI_VERTEXAI=true DL_GEMINI_MODEL=gemini-3.5-flash \
GOOGLE_CLOUD_PROJECT=<project-id> DL_GEMINI_LOCATION=global \
uv run decision-ledger serve
```

The public demo runs on Cloud Run (`asia-east1`, one instance) behind Firebase Hosting, with Gemini on Vertex AI (location `global`). With Google sign-in on (`DL_STORE=firestore`, `DL_AUTH_MODE=firebase`), each workspace's ledger and the usage counters are stored in Firestore, so data survives redeploys; the read-only showcase is stored there too and rebuilt from the recorded scenario only when it is incomplete. Local development defaults to SQLite. Secrets come from Secret Manager. Setup, redeploy steps and known limits are in [`docs/gcp-setup.md`](docs/gcp-setup.md). Business logic depends only on the ports in `domain/ports.py`, so adapters are wired in `bootstrap.py` without touching services.

## Architecture

```
src/decision_ledger/
  domain/     models, ports (LLMClient, Repository, ...), rules (the only decision status transition)
  services/   import, reconciliation, premise watch, PR deviation, alert resolution, Q&A
  adapters/   llm/{fake,gemini,claude_code,recording}, storage/sqlite, github
  api/        FastAPI routes, webhook signature check, RFC 7807 errors
  bootstrap.py  wires backends from environment variables (the only place that picks implementations)
  web/static/   single-page web UI
scripts/      check_no_cjk.py, record_demo.py, eval.py
demo/eval/    gold answers kept outside the runtime package
```

Dependencies point `api -> services -> domain <- adapters`; `tests/test_boundaries.py` checks this with the AST.
