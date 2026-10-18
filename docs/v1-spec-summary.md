# Decision Ledger v1 Spec Summary

Date: 2026-10-17. Scope: everything the first submitted version must contain, including what the prototype already does.

Sources: the team's internal backlog (DL-001 to DL-029), the eight feature specs, and the MVP scope review of 2026-10-15. Every "To do" story below has a prospec change with a frozen acceptance baseline. The backlog, specs and change artifacts are internal planning files written in Traditional Chinese and are not part of the public repository; this summary is in English.

Hours are the MVP review's baseline estimates. The schedule moved from 10/16 to 10/17, so whether everything fits is an open call for the team, not settled here.

## Counts

| Status | Count |
|---|---:|
| Existing (prototype, covered by a feature spec) | 8 |
| v1 to do (prospec change at new-story) | 23 |
| Not in v1 (optional, post-contest, or cut) | 6 |

To-do hours at baseline, after the 10/17 decisions: Ci about 37.25h, Claire about 28.25h (previously 39.25h and 31.25h).

## Existing: built in the prototype

| Capability | Status | Acceptance in one line | Feature spec |
|---|---|---|---|
| Import transcripts and Slack exports, extract decisions with evidence | Existing | Every decision carries at least one verbatim quote with a segment position; invalid LLM output writes nothing | `decision-ingestion.md` |
| Reconcile new vs existing decisions (merge, supersede, conflict, new) | Existing | Conflict and supersede only raise alerts; decision status never changes without a human action | `decision-reconciliation.md` |
| Assumption watch | Existing | A later source that shakes a stated assumption raises an alert with both quotes | `assumption-watch.md` |
| PR deviation detection with comment text, HMAC webhook, idempotency | Existing | A violating diff yields an alert and a comment with decision, rationale, line and source; wrong signature returns 401 | `pr-deviation.md` |
| Topic grouping, rename, merge, move, split | Existing | Decisions are grouped by topic and can be corrected by hand | `decision-topics.md` |
| Alert resolution, scoped Q&A, overview and topic pages | Existing | Decision status changes only after a human resolves an alert | `ledger-workspace.md` |
| Multiple projects with isolated data | Existing | Each project sees only its own decisions and alerts | `project-workspaces.md` |
| Swappable LLM and storage backends, credential-free demo | Existing | Backend chosen by env vars; full demo runs on the Fake backend | `ledger-platform.md` |

## v1 to do: one prospec change each

Next station: `plan` for standard and full scale, `tasks` for quick scale.

| ID | Story | Owner | Est. | Acceptance in one line | Change (scale) |
|---|---|---|---|---|---|
| DL-029 | A violating PR is caught even when the decision's path glob misses the changed file | Claire | 2.5h | On Gemini, PR #87 is judged a violation and PR #88 still passes; one decision counts once and quotes the decision sentence | `fix-deviation-path-fallback` (standard) |
| DL-004 | A violating PR shows an in-product dry-run comment card with quote, source and a product deep link | Ci 1h, Claire 1h | 2h | Within 60 seconds of a violating PR event, the product shows the dry-run card with decision quote, source and deep link; no GitHub write API is called (no comment, no label), while reading the diff from the webhook payload or a read-only API is allowed. Decided 10/17 | `post-github-deviation-comment` (standard; v1 name show-deviation-comment-card, slug kept because the CLI cannot rename) |
| DL-012 | A fictional demo repo with a reproducible violating PR and a compliant one | Ci | 1h | The two branches reproduce fixture PR #87 (violates) and #88 (complies) | `create-demo-violation-repo` (quick) |
| DL-017 | A judge replays the violating PR in their own sandbox without touching GitHub | Claire | 1h | "Replay the violating PR" goes through pr-check; #87 violates, #88 passes, no GitHub call | `replay-violating-pr` (standard) |
| DL-003 | GitHub webhooks get 202 at once and detection runs in the background | Claire | 2h | A valid event returns 202; the same event sent twice posts one comment | `queue-webhook-in-background` (standard) |
| DL-001 | Sandbox registry and counters survive a restart or redeploy; the showcase project rebuilds itself | Claire | 3h | Firestore keeps sandbox registry and counters; after a container recycle the showcase project is rebuilt by rerunning the scenario on Gemini; expired sandboxes say so. Plan B, decided 10/17 | `persist-ledger-across-restarts` (standard) |
| DL-002 | Pin the Gemini model by env and refuse models retiring before 2026-12-04 | Claire | 1h | A retiring model makes `serve` exit with code 2; default is gemini-3.5-flash on location global | `pin-gemini-model-version` (standard) |
| DL-006 | One public URL runs the whole product | Ci | 4h | The Hosting URL loads, the API on Cloud Run answers with no CORS error, secrets live in Secret Manager | `deploy-public-url` (standard) |
| DL-009 | English synthetic scenario labelled Synthetic, with recorded Gemini responses | Ci 5h, Claire 1.5h | 6.5h | The five-step scenario shows all three moments on Gemini; the recorded fixtures turn the e2e test green | `author-english-synthetic-scenario` (standard) |
| DL-010 | A held-out synthetic team used only after freeze | Ci | 2h | 2 meetings, 2 Slack threads, 3 PRs in English, unseen by the labeller before freeze | `build-heldout-scenario-set` (quick) |
| DL-013 | Hand-labelled gold answers stored apart from inputs | Claire 2h, Ci 0.5h | 2h | Gold and inputs live in separate paths; the gold hash is recorded at freeze | `label-heldout-gold-answers` (quick) |
| DL-014 | One command prints hit, false-positive and miss counts for four detectors | Claire | 3h | Extraction, conflict, deviation and assumption counts plus verbatim quote rate; a zero denominator shows N/A | `report-eval-metrics` (standard) |
| DL-015 | Results ship with one real failure case and a synthetic-data disclaimer | Ci | 1h | Deck page and README paragraph, with the failure case taken from an actual run | `publish-failure-case-disclaimer` (quick) |
| DL-018 | Each judge gets an isolated sandbox | Claire 3h, Ci 2h | 5h | Two fresh browsers never see each other's data; one sandbox id cannot be derived from another; writes to the showcase project are refused in public mode. Decided 10/17 | `start-judge-sandbox` (standard) |
| DL-019 | Judge-facing product is fully English | Ci 4h, Claire 1h | 5h | An AST-based CJK scan over `src/`, `web/static/` and `demo/fixtures/` finds nothing | `translate-ui-to-english` (standard) |
| DL-020 | Health endpoint, uptime check and recorded-mode banner | Ci 1h, Claire 1h | 2h | `GET /api/health` (also `/healthz` in-container) returns 200 without calling the LLM; uptime failures notify | `monitor-judging-period` (standard) |
| DL-021 | Per-sandbox and site-wide daily LLM limits | Claire | 2h | Over the limit returns 429 problem+json and the LLM is not called | `limit-sandbox-llm-usage` (standard) |
| DL-022 | Model failures show a clear error and retry, never a fake success | Claire | 1h | LLM failure returns 502 with no write; the UI shows error and retry | `surface-llm-failure-retry` (standard) |
| DL-023 | Deck with architecture, measured counts and limits | Ci | 6h | Official template, PDF at most 5MB, every number traceable to an eval run | `build-submission-deck` (quick) |
| DL-024 | Demo video under three minutes with all three moments live in the cloud | Ci 6h, Claire 1h | 7h | Under 3:00, edits labelled, any fallback footage labelled "Recorded run" | `record-demo-video` (quick) |
| DL-025 | 1024-character description of what each Google Cloud service does | Ci | 1.5h | At most 1024 characters; Hosting, Firestore, Cloud Run and Gemini each get a paragraph; no ADK claim | `write-cloud-usage-description` (quick) |
| DL-026 | New public repo with clean history and written Fresh Code Base evidence | Ci 1.5h, Claire 1.5h | 3h | English HEAD, no secrets, no tool config, history scan clean | `publish-clean-public-repo` (quick) |
| DL-027 | Pre-submission checklist | Ci 0.75h, Claire 0.75h | 1.5h | Links, video length, character count, model, secrets and a clean smoke test each have evidence | `run-presubmission-checklist` (quick) |

The PR deviation chain in v1 is DL-029, DL-004, DL-012, DL-017 and DL-003. DL-016 is not in v1.

## Not in v1

| ID | Story | Why not |
|---|---|---|
| DL-005 | Judge sign-in | Not in the original v1 scope; implemented later by DL-032 (Google sign-in with Firebase Authentication, one workspace per account). Anonymous sandboxes are off in sign-in mode |
| DL-007 | Progress for long imports | Cut. Each step is its own request and Cloud Run has no 60-second limit |
| DL-008 | Semantic recall | Post-contest. None of the three moments depend on it |
| DL-011 | Play meeting audio from a quote | Optional. Text highlight already proves traceability |
| DL-016 | Deterministic import check before the LLM | Post-contest. DL-029 is the minimal fix for the demo |
| DL-028 | English Q&A | Optional. Hidden in public mode and left out of the video |
