#!/usr/bin/env bash
# Export a clean, single-commit copy of the integration branch for the public repo (DL-026).
# Usage: scripts/export_public_repo.sh <target-dir> [ref]     (ref defaults to HEAD)
# Only tracked files are exported (git archive), so .venv, caches and untracked files never leak.
# Nothing is pushed and no GitHub repo is created.
set -euo pipefail

TARGET="${1:?usage: $0 <target-dir> [ref]}"
REF="${2:-HEAD}"
SRC="$(git rev-parse --show-toplevel)"
[[ -e "$TARGET" ]] && { echo "target exists: $TARGET (choose a new directory)" >&2; exit 2; }

# Internal process files, tool configuration and deployment notes stay private.
# Internal planning files (Traditional Chinese specs, knowledge base, backlog, market and stack notes)
# stay private too: the contest requires the public repo in English. deploy/ and tests/ ship
# (tests keep their CJK fixtures on purpose; they exercise the CJK handling).
EXCLUDE=(
  .prospec .prospec.yaml .claude .firebase .firebaserc
  AGENTS.md CLAUDE.md
  docs/gcp-setup.md
  prospec backlog.md docs/market-analysis.md docs/tech-stack-decision.md
)

mkdir -p "$TARGET"
git -C "$SRC" archive --format=tar "$REF" | tar -x -C "$TARGET"
for path in "${EXCLUDE[@]}"; do rm -rf "${TARGET:?}/$path"; done
rm -f "$TARGET"/docs/overnight-*.md
find "$TARGET" \( -name '.env' -o -name '.env.*' \) ! -name '*.example' -exec rm -f {} +

# Author: AUTHOR_NAME/AUTHOR_EMAIL if set; the e-mail defaults to the GitHub no-reply address so no
# personal address enters the public history; falls back to the local git config.
NAME="${AUTHOR_NAME:-$(git -C "$SRC" config user.name)}"
if [[ -z "${AUTHOR_EMAIL:-}" ]]; then
  if ! command -v gh >/dev/null || ! LOGIN_ID="$(gh api user --jq '"\(.id)+\(.login)"' 2>/dev/null)"; then
    echo "gh is missing or not logged in, so the GitHub no-reply e-mail is unknown; run 'gh auth login' or set AUTHOR_EMAIL" >&2
    rm -rf "${TARGET:?}"
    exit 1
  fi
  AUTHOR_EMAIL="${LOGIN_ID}@users.noreply.github.com"
fi
EMAIL="${AUTHOR_EMAIL:-$(git -C "$SRC" config user.email)}"
if [[ "$EMAIL" != *@users.noreply.github.com && "${ALLOW_PERSONAL_EMAIL:-0}" != 1 ]]; then
  echo "author e-mail is not a GitHub no-reply address; log in with gh, set AUTHOR_EMAIL, or ALLOW_PERSONAL_EMAIL=1" >&2
  rm -rf "${TARGET:?}"
  exit 1
fi
SOURCE_SHA="$(git -C "$SRC" rev-parse --short "$REF")"

git -C "$TARGET" init -q -b main
git -C "$TARGET" add -A
git -C "$TARGET" -c user.name="$NAME" -c user.email="$EMAIL" commit -q \
  -m "Decision Ledger: initial public release" \
  -m "Exported from the private development repository at ${SOURCE_SHA}."
echo "exported ${REF}@${SOURCE_SHA} -> ${TARGET} ($(git -C "$TARGET" ls-files | wc -l | tr -d ' ') files, 1 commit)"
