# Fresh Code Base statement

Statement date: 2026-10-17. Repository facts below were read from `git` on that date.

## Repository facts (verifiable with git)

| Fact | Value | How to check |
|---|---|---|
| First commit | `03753a54ed9d52d4b7cb549077846a9d5ddfe394` (88 files) | `git log --all --reverse --format='%H %aI' \| head -1` |
| First commit date | 2026-10-13 14:56 (UTC+8) | same command |
| Contest launch date | to be attached (hack2skill contest page) | hack2skill contest page |
| Authors (GitHub accounts) | `ci-yang`, `claire0613` (Claire's commits appear under the display names `claire0613` and `claire`, both from the GitHub account `claire0613`) | `git log --all --format='%an %ae'` |

git proves the first commit's date and content (it adds a complete local MVP, 88 files, at once), not when or where the code was written; the signed declaration below is the authors' confirmation of that.

**Excluded from the public repo**: `prospec/`, `backlog.md`, `docs/market-analysis.md` and `docs/tech-stack-decision.md` (internal planning files in Traditional Chinese; the contest requires English). `deploy/` and `tests/` ship.

Public repository: https://github.com/fish-in-waters-cjp/decision-ledger-cup. The public repository is a single-commit snapshot export of the integration branch; the export commit message records the exact source SHA (`scripts/export_public_repo.sh`). First export 2026-10-17 from `23145e5`; re-exported after the sign-in and i18n releases. The author is set to the GitHub no-reply address. The first export was checked on a fresh clone: `scripts/scan_history.sh` reported no hits and gitleaks 8.30.1 reported no leaks. The full development history stays available to the organisers on request.

## Third-party packages

All dependencies are open-source packages installed from PyPI and pinned in `uv.lock`.

| Package | Purpose | Scope |
|---|---|---|
| fastapi | HTTP API and static web UI | runtime |
| pydantic | data models and LLM output validation | runtime |
| python-multipart | file uploads | runtime |
| uvicorn[standard] | ASGI server | runtime |
| google-genai | Gemini on Vertex AI | optional extra `gemini` |
| google-cloud-firestore | ledger, usage counters and sandbox registry | optional extra `cloud` |
| google-auth, requests | Firebase ID token verification | optional extra `cloud` |
| httpx | test client | dev |
| pytest | tests | dev |
| pytest-socket | blocks network access in tests | dev |

The web UI uses no front-end framework or npm packages; it loads fonts from Google Fonts and, when Google sign-in is on, the Firebase JS SDK (pinned 10.14.1) from Google's CDN.

## Declaration

All code in this repository was written for this contest, starting with the first commit on 2026-10-13; no code predates the contest and no code was copied from prior projects. Development was AI-assisted with coding agents under the authors' direction.

| Author | GitHub | Signature | Date |
|---|---|---|---|
| Ci | `ci-yang` | | |
| Claire | `claire0613` | | |
