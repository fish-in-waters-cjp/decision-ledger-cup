#!/usr/bin/env bash
# Scan every commit on every branch for secrets and identifiers that must not go public (DL-026).
# Prints pattern names, file paths and short commit SHAs only, never the matched values.
# Usage: scripts/scan_history.sh [repo-dir]     exit 0 = no hits, 1 = hits or missing private patterns, 2 = usage or pattern error
# Private identifiers (one regex per line) are read from $SCAN_PRIVATE_PATTERNS, default
# .scan-private-patterns at the repo root (gitignored), so this script never carries them itself.
set -euo pipefail
REPO="${1:-.}"
# Resolved next to this script (not the scanned repo), so scanning an export still loads the patterns.
SCRIPT_REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
PRIVATE="${SCAN_PRIVATE_PATTERNS:-$SCRIPT_REPO/.scan-private-patterns}"
if [[ ! -f "$PRIVATE" && "${SCAN_ALLOW_NO_PRIVATE:-0}" != 1 ]]; then
  echo "private pattern file not found: $PRIVATE (set SCAN_PRIVATE_PATTERNS, or SCAN_ALLOW_NO_PRIVATE=1)" >&2
  exit 1
fi
export PRIVATE
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || { echo "not a git repo: $REPO" >&2; exit 2; }

python3 - "$REPO" <<'PY'
import re, subprocess, sys
from collections import defaultdict

repo = sys.argv[1]
PATTERNS = {
    "billing-id": r"(?i)(?<![0-9a-z])[0-9a-f]{6}-[0-9a-f]{6}-[0-9a-f]{6}(?![0-9a-z])",
    "aws-access-key": r"AKIA[0-9A-Z]{16}",
    "anthropic-key": r"sk-ant-[0-9A-Za-z_-]{20,}",
    "openai-key": r"(?<![0-9A-Za-z_-])sk-(proj-)?[0-9A-Za-z_-]{20,}",
    "service-account-json": r"\"type\"\s*:\s*\"service_account\"|\"private_key_id\"\s*:",
    "project-number-12-digits": r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "google-api-key": r"AIza[0-9A-Za-z_-]{35}",
    "github-token": r"gh[pousr]_[0-9A-Za-z]{20,}",
    "private-key": "-" * 5 + "BEGIN",  # assembled so this file does not match itself
}
import os
if os.path.isfile(os.environ.get("PRIVATE", "")):
    for i, line in enumerate(l.strip() for l in open(os.environ["PRIVATE"], encoding="utf-8")):
        if line and not line.startswith("#"):
            PATTERNS[f"private-pattern-{i + 1}"] = line
else:
    print("note: no private pattern file; scanning generic patterns only", file=sys.stderr)
# Addresses that are public by design (git hosting no-reply, documentation placeholders).
EMAIL_ALLOW = re.compile(r"@(users\.noreply\.github\.com|anthropic\.com|example\.(com|org|net))$", re.I)
try:
    compiled = {k: re.compile(v) for k, v in PATTERNS.items()}
except re.error as exc:  # a broken private pattern must not look like a clean scan
    print(f"invalid pattern: {exc}", file=sys.stderr)
    sys.exit(2)

def git(*args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace").stdout

hits = defaultdict(set)   # (pattern, path) -> {short sha}
seen_blobs = {}           # blob sha -> list of pattern names (scan each blob once)
commits = git("rev-list", "--all").split()
for commit in commits:
    short = commit[:7]
    for line in git("ls-tree", "-r", commit).splitlines():
        meta, path = line.split("\t", 1)
        _, kind, blob = meta.split()
        if kind != "blob":
            continue
        if re.search(r"(^|/)\.env($|\.)", path) and not path.endswith(".example"):
            hits[("dotenv-file", path)].add(short)
        if blob not in seen_blobs:
            text = git("cat-file", "-p", blob)
            found = []
            for name, rx in compiled.items():
                matches = rx.findall(text) if name != "email" else [
                    m for m in rx.findall(text) if not EMAIL_ALLOW.search(m)]
                if matches:
                    found.append(name)
            seen_blobs[blob] = found
        for name in seen_blobs[blob]:
            hits[(name, path)].add(short)

# Commit and tag messages are scanned with the same patterns.
for line in git("log", "--all", "--format=%h%x00%B%x01").split("\x01"):
    if "\x00" not in line:
        continue
    short, body = line.strip("\n").split("\x00", 1)
    for name, rx in compiled.items():
        found = rx.findall(body) if name != "email" else [m for m in rx.findall(body) if not EMAIL_ALLOW.search(m)]
        if found:
            hits[(name, "(commit message)")].add(short.strip())
for tag in git("tag").split():
    body = git("cat-file", "-p", tag) if git("cat-file", "-t", tag).strip() == "tag" else ""
    for name, rx in compiled.items():
        found = rx.findall(body) if name != "email" else [m for m in rx.findall(body) if not EMAIL_ALLOW.search(m)]
        if found:
            hits[(name, f"(tag {tag})")].add(tag)

# Commit metadata: author / committer e-mails that are not no-reply addresses.
for line in git("log", "--all", "--format=%h %ae %ce").splitlines():
    short, *mails = line.split()
    for m in mails:
        if not EMAIL_ALLOW.search(m):
            hits[("author-email-metadata", "(commit metadata)")].add(short)

order = {c[:7]: i for i, c in enumerate(reversed(commits))}
print(f"scanned {len(commits)} commits, {len(seen_blobs)} unique blobs")
if not hits:
    print("no hits")
    sys.exit(0)
for (name, path), shas in sorted(hits.items()):
    s = sorted(shas, key=lambda x: order.get(x, 0))
    print(f"HIT {name:26} {path}  commits={len(s)} first={s[0]} last={s[-1]}")
print(f"{len(hits)} (pattern, path) hit(s)")
sys.exit(1)
PY
