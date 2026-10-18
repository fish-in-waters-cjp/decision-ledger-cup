"""The demo PR fixtures match the synthetic GitHub repository (DL-012), checked offline."""

import json

from decision_ledger.bootstrap import DEMO_DIR

REPO = "ci-yang/decision-ledger-demo-repo"


def pull_requests():
    manifest = json.loads((DEMO_DIR / "manifest.json").read_text(encoding="utf-8"))
    [step] = [s for s in manifest["steps"] if s["type"] == "prs"]
    return {pr["pr_number"]: pr for pr in step["pull_requests"]}


def added_lines(diff: str) -> list[str]:
    return [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


def test_both_pull_requests_point_at_the_demo_repository():
    prs = pull_requests()
    assert set(prs) == {87, 88}
    assert {pr["repo"] for pr in prs.values()} == {REPO}
    assert all(len(pr["head_sha"]) == 40 and int(pr["head_sha"], 16) >= 0 for pr in prs.values())


def test_pr_87_imports_the_database_client_inside_payments():
    diff = (DEMO_DIR / pull_requests()[87]["file"]).read_text(encoding="utf-8")
    assert "+++ b/src/payments/refunds.ts" in diff
    assert any("import { dbClient }" in line for line in added_lines(diff))


def test_pr_88_stays_on_the_ledger_api():
    diff = (DEMO_DIR / pull_requests()[88]["file"]).read_text(encoding="utf-8")
    touched = {line[6:] for line in diff.splitlines() if line.startswith("+++ b/")}
    assert touched == {"src/payments/format.ts"}
    assert not any("dbClient" in line for line in added_lines(diff))
