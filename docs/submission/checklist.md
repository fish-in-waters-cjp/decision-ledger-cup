# Pre-submission checklist

> **Draft, Ci to run before submitting.** Run `scripts/presubmit_check.sh` with every input available; paste its output as evidence. Any FAIL means do not submit: go back to the story named in the last column. Every SKIP must be resolved by hand or justified.

```bash
scripts/presubmit_check.sh --video demo.mp4 --deck deck.pdf \
  --repo-url https://github.com/fish-in-waters-cjp/decision-ledger-cup --video-url https://youtu.be/<id>
```

| # | Item | Command | Evidence | Owner | Story if it fails |
|---|---|---|---|---|---|
| 1 | Public site works for its `auth_mode` (pages, health, runtime config, read-only showcase; sign-in mode: unauthenticated requests refused; anonymous mode: sandbox create, page, reset, expired) | `scripts/smoke_cloud.sh` (reads `/api/runtime`; anonymous mode creates and resets one sandbox per run) | PASS lines, SKIP only for checks of the other mode | Ci | DL-006, DL-018, DL-032 |
| 2 | Live Gemini model is allow-listed and retires after 2026-12-04 | `/api/health` `model` checked with `check_gemini_model` | PASS line naming the model | Ci | DL-002 |
| 3 | Demo video under 180 s (target 170 s or less) | `ffprobe` duration | PASS line with seconds | Ci | DL-024 |
| 4 | Deck PDF 5 MB or smaller | file size | PASS line with bytes | Ci | DL-023 |
| 5 | Cloud usage description: 1,024 characters or fewer, names Firebase, Firestore, Cloud Run and Gemini | `wc -m docs/submission/description.md` | PASS line with character count | Ci | DL-025 |
| 6 | Judge-facing text is English only | `scripts/check_no_cjk.py src docs/submission` | PASS line | Claire | DL-019 |
| 7 | No secrets or private identifiers in history | `scripts/scan_history.sh .` (needs `.scan-private-patterns`) | PASS line; only author e-mail metadata may remain in the private repo | Ci | DL-026 |
| 8 | Tests pass in a clean clone | `git clone` + `uv run pytest -q` | PASS line with pytest summary | Claire | the failing test's story |
| 9 | Repo and video links open for a logged-out visitor | `curl -sL` returns 200; YouTube links are checked through the oEmbed endpoint (a missing video still returns 200 on the watch page) | PASS lines; also open both in a private window | Ci | DL-026, DL-024 |

## Public repository

https://github.com/fish-in-waters-cjp/decision-ledger-cup (public, branch `main`). The public repository is a single-commit snapshot export of the integration branch; the export commit message records the exact source SHA (`scripts/export_public_repo.sh`). First export 2026-10-17 from `23145e5`; re-exported after the sign-in and i18n releases. The author is set to the GitHub no-reply address. The first export was checked on a fresh clone: `scripts/scan_history.sh` reported no hits and gitleaks 8.30.1 reported no leaks.

## Last run (2026-10-17 overnight, Lane B, without video, deck, description or links)

```
PASS  1 public site smoke  (all checks passed)
PASS  2 live Gemini model  (gemini-3.5-flash is allow-listed)
SKIP  3 video length  (no --video given)
SKIP  4 deck size  (no --deck given)
SKIP  5 description  (docs/submission/description.md not written yet)
SKIP  6 English only  (scripts/check_no_cjk.py not merged yet (Lane C))
PASS  7 secret scan  (no content hits; author e-mails in private history only (public export uses no-reply))
PASS  8 clean clone tests  (461 passed, 2 skipped, 1 warning in 1.92s)
SKIP  9 repo link  (no --repo-url given)
SKIP  9 video link  (no --video-url given)
```
