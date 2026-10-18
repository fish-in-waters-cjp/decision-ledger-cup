"""Runs the browser-module tests with Node's built-in runner (skipped when Node is not installed)."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_api_js_node_tests():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    run = subprocess.run([node, "--test", "tests/web/api.test.mjs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr


def test_gateway_timeout_sentence_is_in_api_js():
    # DL-031: the sentence may live in locales/en.js with api.js referring to its key.
    from tests.ui_text import shows

    api = ROOT / "src/decision_ledger/web/static/api.js"
    assert shows(api, "The model took longer than 60 seconds. Retry, or wait a moment and refresh.")
    assert "new Set([502, 503, 504])" in api.read_text(encoding="utf-8")


def test_conflict_card_title_does_not_assume_meetings():
    """A conflict can come from a Slack thread (showcase decision #5), so the card title is source-neutral."""
    from tests.ui_text import english, shows

    overview = ROOT / "src/decision_ledger/web/static/overview.js"
    assert shows(overview, "Two sources made conflicting decisions")  # DL-031: literal or its en.js key
    assert "Two meetings made conflicting decisions" not in overview.read_text(encoding="utf-8")
    assert "Two meetings made conflicting decisions" not in english().values()
