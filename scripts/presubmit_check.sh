#!/usr/bin/env bash
# Pre-submission checklist (DL-027). Each check prints PASS / FAIL / SKIP; any FAIL exits 1.
# Usage: scripts/presubmit_check.sh [--base-url URL] [--video PATH] [--deck PDF]
#                                   [--repo-url URL] [--video-url URL]
# Network checks hit the public site only through scripts/smoke_cloud.sh and /api/health; no Gemini call.
set -uo pipefail

ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
BASE_URL="https://decision-ledger-cup.web.app"; VIDEO=""; DECK=""; REPO_URL=""; VIDEO_URL=""
while (( $# )); do
  case "$1" in
    --base-url) BASE_URL="$2"; shift 2 ;;
    --video) VIDEO="$2"; shift 2 ;;
    --deck) DECK="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --video-url) VIDEO_URL="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
BASE_URL="${BASE_URL%/}"
FAILS=0
pass() { echo "PASS  $1${2:+  ($2)}"; }
fail() { echo "FAIL  $1${2:+  ($2)}"; FAILS=$((FAILS + 1)); }
skip() { echo "SKIP  $1${2:+  ($2)}"; }

# 1. Public site
if "$ROOT/scripts/smoke_cloud.sh" "$BASE_URL" >/tmp/dl-presubmit-smoke.$$ 2>&1; then
  pass "1 public site smoke" "$(tail -1 /tmp/dl-presubmit-smoke.$$)"
else
  fail "1 public site smoke" "$(grep -c '^FAIL' /tmp/dl-presubmit-smoke.$$) failing check(s); run scripts/smoke_cloud.sh"
fi
rm -f /tmp/dl-presubmit-smoke.$$

# 2. Live model is allow-listed and retires after the judging cutoff (same guard serve uses).
MODEL="$(curl -s "$BASE_URL/api/health" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("model") or "")' 2>/dev/null)"
if [[ -z "$MODEL" ]]; then
  fail "2 live Gemini model" "no model in $BASE_URL/api/health"
elif OUT="$(cd "$ROOT" && DL_CHECK_MODEL="$MODEL" uv run --quiet python -c "import os; from decision_ledger.bootstrap import check_gemini_model as c; c(os.environ['DL_CHECK_MODEL'])" 2>&1)"; then
  pass "2 live Gemini model" "$MODEL is allow-listed"
else
  fail "2 live Gemini model" "$(echo "$OUT" | tail -1)"
fi

# 3. Video under 180 s (target <= 170 s)
if [[ -z "$VIDEO" ]]; then
  skip "3 video length" "no --video given"
elif ! command -v ffprobe >/dev/null; then
  fail "3 video length" "ffprobe not installed, so the given video cannot be checked (install ffmpeg, e.g. brew install ffmpeg)"
elif [[ ! -f "$VIDEO" ]]; then
  fail "3 video length" "file not found: $VIDEO"
else
  SECS="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$VIDEO" 2>/dev/null)"
  if ! python3 -c "float('$SECS')" 2>/dev/null; then
    fail "3 video length" "could not read a duration from $VIDEO"
  elif python3 -c "import sys; sys.exit(0 if float('$SECS') < 180 else 1)"; then
    NOTE="$(python3 -c "s=float('$SECS'); print(f'{s:.1f}s' + ('' if s <= 170 else ', over the 170s target'))")"
    pass "3 video length" "$NOTE"
  else
    fail "3 video length" "${SECS}s is not under 180s"
  fi
fi

# 4. Deck PDF <= 5 MB
if [[ -z "$DECK" ]]; then
  skip "4 deck size" "no --deck given"
elif [[ ! -f "$DECK" ]]; then
  fail "4 deck size" "file not found: $DECK"
elif [[ "$(file -b --mime-type "$DECK" 2>/dev/null)" != application/pdf ]]; then
  fail "4 deck size" "$DECK is not a PDF ($(file -b --mime-type "$DECK" 2>/dev/null))"
else
  BYTES="$(wc -c <"$DECK" | tr -d ' ')"
  (( BYTES <= 5 * 1024 * 1024 )) && pass "4 deck size" "$BYTES bytes" || fail "4 deck size" "$BYTES bytes > 5 MB"
fi

# 5. Cloud usage description
DESC="$ROOT/docs/submission/description.md"
if [[ ! -f "$DESC" ]]; then
  skip "5 description" "docs/submission/description.md not written yet"
else
  CHARS="$(wc -m <"$DESC" | tr -d ' ')"
  MISSING=""
  for word in Firebase Firestore "Cloud Run" Gemini; do grep -q "$word" "$DESC" || MISSING+="${MISSING:+, }$word"; done
  if (( CHARS <= 1024 )) && [[ -z "$MISSING" ]]; then pass "5 description" "$CHARS chars"
  else fail "5 description" "$CHARS chars (limit 1024)${MISSING:+; missing: $MISSING}"; fi
fi

# 6. Judge-facing English
if [[ ! -f "$ROOT/scripts/check_no_cjk.py" ]]; then
  skip "6 English only" "scripts/check_no_cjk.py not merged yet (Lane C)"
elif OUT="$(cd "$ROOT" && uv run --quiet python scripts/check_no_cjk.py src docs/submission 2>&1)"; then
  pass "6 English only" "src and docs/submission"
else
  fail "6 English only" "$(echo "$OUT" | tail -1)"
fi

# 7. Secrets in history
if [[ ! -f "${SCAN_PRIVATE_PATTERNS:-$ROOT/.scan-private-patterns}" ]]; then
  skip "7 secret scan" "create .scan-private-patterns (one private regex per line, gitignored) or set SCAN_PRIVATE_PATTERNS"
else
  OUT="$("$ROOT/scripts/scan_history.sh" "$ROOT" 2>&1)"; RC=$?
  HITS="$(echo "$OUT" | grep '^HIT' | grep -v 'author-email-metadata')"
  if (( RC > 1 )) || ! echo "$OUT" | grep -q '^scanned [0-9]* commits'; then
    fail "7 secret scan" "scanner did not complete (exit $RC): $(echo "$OUT" | tail -1)"
  elif [[ -z "$HITS" ]]; then
    pass "7 secret scan" "no content hits$(echo "$OUT" | grep -q author-email-metadata && echo '; author e-mails in private history only (public export uses no-reply)')"
  else
    fail "7 secret scan" "$(echo "$HITS" | wc -l | tr -d ' ') hit(s); run scripts/scan_history.sh"
  fi
fi

# 8. Clean-environment tests from a fresh clone of HEAD
TMP="$(mktemp -d)"
if git clone -q "$ROOT" "$TMP/repo" 2>/dev/null && OUT="$(cd "$TMP/repo" && uv run --quiet pytest -q 2>&1)"; then
  pass "8 clean clone tests" "$(echo "$OUT" | tail -1)"
elif [[ -d "$TMP/repo" ]]; then
  fail "8 clean clone tests" "$(echo "$OUT" | tail -1)"
else
  skip "8 clean clone tests" "could not clone"
fi
rm -rf "$TMP"

# 9. Submission links
for pair in "repo:$REPO_URL" "video:$VIDEO_URL"; do
  name="${pair%%:*}"; url="${pair#*:}"
  if [[ -z "$url" ]]; then skip "9 $name link" "no --$name-url given"; continue; fi
  CHECK="$url"
  # YouTube answers 200 even for a missing video; its oEmbed endpoint returns 4xx for those.
  if [[ "$url" =~ (youtube\.com|youtu\.be) ]]; then
    CHECK="https://www.youtube.com/oembed?format=json&url=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$url")"
  fi
  CODE="$(curl -sL -o /dev/null -w '%{http_code}' "$CHECK")"
  [[ "$CODE" == 200 ]] && pass "9 $name link" "$url" || fail "9 $name link" "HTTP $CODE for $CHECK"
done

echo "---"
if (( FAILS )); then echo "$FAILS check(s) failed: do not submit"; exit 1; fi
echo "no failures (review every SKIP before submitting)"
