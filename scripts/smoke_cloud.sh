#!/usr/bin/env bash
# Smoke test the public deployment without calling Gemini. Prints PASS/FAIL/SKIP per check; exit 1 on any FAIL.
# Usage: scripts/smoke_cloud.sh [base_url]
# The mode comes from /api/runtime: anonymous sandboxes (auth_mode none) or Google sign-in (auth_mode firebase,
# DL-032). SMOKE_EXPECT_PUBLIC=0 skips the public-mode expectation (local runs with DL_AUTH_MODE=fake, which
# cannot be combined with DL_PUBLIC_MODE=1).
set -uo pipefail

BASE="${1:-https://decision-ledger-cup.web.app}"
BASE="${BASE%/}"
EXPECT_PUBLIC="${SMOKE_EXPECT_PUBLIC:-1}"
FAILS=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

check() {  # check <name> <condition-exit-code>
  if [[ "$2" == 0 ]]; then echo "PASS  $1"; else echo "FAIL  $1"; FAILS=$((FAILS + 1)); fi
}
skip() { echo "SKIP  $1"; }
code() { curl -s -o "$TMP/body" -w '%{http_code}' "$@"; }
json() { python3 -c "import json,sys; d=json.load(open('$TMP/body')); print($1)" 2>/dev/null; }

[[ "$(code "$BASE/")" == 200 ]]; check "GET / is 200" $?

[[ "$(code "$BASE/api/health")" == 200 ]]; check "GET /api/health is 200" $?
SHOWCASE="$(json "d.get('showcase_project') or ''")"
PUBLIC="$(json "d.get('public_mode')")"
[[ "$(json "d.get('showcase')")" == ready ]]; check "showcase is ready" $?
if [[ "$EXPECT_PUBLIC" == 1 ]]; then [[ "$PUBLIC" == True ]]; check "public mode is on" $?; else skip "public mode is on (SMOKE_EXPECT_PUBLIC=0)"; fi

# Mode and sign-in config. /api/runtime exists since DL-032; an older deployment means anonymous sandboxes.
RT="$(code "$BASE/api/runtime")"
if [[ "$RT" == 200 ]]; then
  MODE="$(json "d.get('auth_mode') or 'none'")"
  # firebase holds only apiKey (public by design), authDomain and projectId; values are never printed.
  python3 - "$TMP/body" "$MODE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); mode = sys.argv[2]; fb = d.get("firebase")
if mode == "firebase":
    ok = isinstance(fb, dict) and bool(fb.get("authDomain")) and set(fb) <= {"apiKey", "authDomain", "projectId"}
else:
    ok = fb in (None, {})
sys.exit(0 if ok else 1)
PY
  RC=$?
  if [[ "$MODE" == firebase ]]; then
    check "GET /api/runtime sign-in config: authDomain present, no fields beyond apiKey/authDomain/projectId" $RC
  else
    check "GET /api/runtime ($MODE): no Firebase config exposed" $RC
  fi
else
  MODE=none
  skip "GET /api/runtime (HTTP $RT: deployment predates sign-in, treated as anonymous sandboxes)"
fi

if [[ "$MODE" == none ]]; then
  [[ "$(code -X POST "$BASE/api/sandboxes")" == 201 ]]; check "POST /api/sandboxes is 201" $?
  SID="$(json "d['id']")"
  [[ "$SID" =~ ^[A-Za-z0-9_-]{16}$ ]]; check "sandbox id is 16 url-safe chars" $?
  [[ "$(code "$BASE/s/$SID/")" == 200 ]] && grep -q 'id="view"' "$TMP/body"; check "sandbox page is 200" $?
  [[ "$(code "$BASE/api/sandboxes/$SID")" == 200 ]]; check "GET sandbox is 200" $?
else
  [[ "$(code "$BASE/api/me")" == 401 ]]; check "GET /api/me without a token is 401" $?
  S="$(code -X POST "$BASE/api/sandboxes")"
  [[ "$S" == 401 || "$S" == 403 || "$S" == 404 ]]; check "POST /api/sandboxes is refused in sign-in mode ($S)" $?
  skip "sandbox id format (no anonymous sandboxes in sign-in mode)"
  skip "sandbox page (no anonymous sandboxes in sign-in mode)"
fi

# A write to the showcase must be refused before any LLM call (topic rename needs no LLM anyway).
W="$(code -X POST -H 'Content-Type: application/json' -d '{"name":"smoke"}' "$BASE/api/projects/${SHOWCASE:-P1}/topics/T1/rename")"
if [[ "$PUBLIC" == True || "$MODE" == none ]]; then [[ "$W" == 403 ]]; else [[ "$W" == 401 || "$W" == 403 ]]; fi
check "write to showcase is refused ($W)" $?
[[ "$(code -X POST -H 'Content-Type: application/json' -d '{"name":"smoke"}' "$BASE/api/projects")" =~ ^40[13]$ ]]
check "POST /api/projects without sign-in is refused" $?

if [[ "$MODE" == none ]]; then
  [[ "$(code -X POST "$BASE/api/sandboxes/$SID/reset")" == 204 ]]; check "sandbox reset is 204" $?
else
  skip "sandbox reset (no anonymous sandboxes in sign-in mode)"
fi

EXPIRED="$(code "$BASE/api/sandboxes/AAAAAAAAAAAAAAAA")"
if [[ "$MODE" == none ]]; then
  [[ "$EXPIRED" == 404 || "$EXPIRED" == 410 ]] && [[ "$(json "d.get('detail')")" == "Sandbox expired, start a new one" ]]
  check "unknown sandbox says expired" $?
else
  [[ "$EXPIRED" =~ ^40[134]$ ]]; check "sandbox lookup is refused in sign-in mode ($EXPIRED)" $?
fi

echo "---"
if (( FAILS )); then echo "$FAILS check(s) failed"; exit 1; fi
echo "all checks passed"
