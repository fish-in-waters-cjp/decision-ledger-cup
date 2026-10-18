"""Architecture rules enforced by static analysis (Constitution: swappable LLM / storage, clean boundaries, HITL)."""

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "src" / "decision_ledger"

FORBIDDEN = {
    "domain": ("decision_ledger.services", "decision_ledger.adapters", "decision_ledger.api", "decision_ledger.bootstrap",
               "sqlite3", "google", "subprocess", "fastapi", "starlette", "urllib", "uvicorn"),
    "services": ("decision_ledger.adapters", "decision_ledger.api", "decision_ledger.bootstrap",
                 "sqlite3", "google", "subprocess", "fastapi", "starlette", "urllib", "uvicorn"),
    "api": ("decision_ledger.adapters", "decision_ledger.bootstrap", "sqlite3", "google", "subprocess"),
}


def imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def violations(layer: str, root: Path = PKG) -> list[str]:
    found = []
    for path in sorted((root / layer).rglob("*.py")):
        for name in imports(path):
            if any(name == f or name.startswith(f + ".") for f in FORBIDDEN[layer]):
                found.append(f"{path.relative_to(root)} imports {name}")
    return found


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_imports_respect_dependency_direction(layer):
    assert violations(layer) == []


def test_boundary_check_detects_a_violation(tmp_path):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "bad.py").write_text("import sqlite3\nfrom decision_ledger.adapters.github import x\n")
    assert len(violations("services", tmp_path)) == 2


def callers_of(name: str, root: Path = PKG) -> set[str]:
    found = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                if called == name:
                    found.add(str(path.relative_to(root)))
    return found


def test_only_alert_service_changes_decision_status():
    """REQ-LEDGER-WORKSPACE-001 AC7: state transitions happen only through the user's disposition."""
    assert callers_of("apply_transition") == {"services/alerts.py"}


def test_no_code_sets_commit_status():
    """REQ-PR-DEVIATION-001 AC4: advisory only — no commit status or check-run endpoints anywhere."""
    for path in PKG.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "/statuses/" not in text and "check-runs" not in text, path


def test_topic_changes_never_create_alerts():
    """REQ-TOPIC-002 AC3: assigning, renaming and merging topics only move decisions between topics."""
    assert "services/topics.py" not in callers_of("Alert") | callers_of("apply_transition")
