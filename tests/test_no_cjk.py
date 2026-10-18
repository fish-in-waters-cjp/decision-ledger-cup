"""REQ-LEDGER-PLATFORM-028: no CJK text in anything a judge can see (scripts/check_no_cjk.py)."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_no_cjk", ROOT / "scripts" / "check_no_cjk.py")
check_no_cjk = importlib.util.module_from_spec(_spec)
sys.modules["check_no_cjk"] = check_no_cjk  # dataclasses look the module up while the class is created
_spec.loader.exec_module(check_no_cjk)



def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def lines(hits) -> list[int]:
    return [h.line for h in hits]


def test_python_string_constants_are_reported_with_their_line(tmp_path):
    write(tmp_path, "m.py", 'X = "ok"\nTITLE = "錯誤"\nY = f"{X} 測試"\n')
    assert lines(check_no_cjk.scan(tmp_path)) == [2, 3]


def test_python_comments_docstrings_and_escapes_are_not_reported(tmp_path):
    source = (
        '"""模組說明"""\n'
        "# 註解\n"
        "def f():\n"
        '    """函式說明"""\n'
        '    return "\\uff1a"  # 全形冒號\n'
    )
    write(tmp_path, "m.py", source)
    assert check_no_cjk.scan(tmp_path) == []


def test_html_text_and_attributes_are_reported_but_comments_are_not(tmp_path):
    html = '<html lang="en">\n<!-- 註解 -->\n<p>匯入</p>\n<aside aria-label="出處"></aside>\n</html>\n'
    write(tmp_path, "index.html", html)
    assert lines(check_no_cjk.scan(tmp_path)) == [3, 4]


def test_json_strings_and_whole_text_files_are_reported(tmp_path):
    write(tmp_path, "a.json", '{\n  "ok": "fine",\n  "title": "週會"\n}\n')
    write(tmp_path, "b.js", "const a = 1;\n// 註解\n")
    write(tmp_path, "c.txt", "[00:00:01] A: ：ok\n")
    hits = check_no_cjk.scan(tmp_path)
    assert sorted((h.path.name, h.line) for h in hits) == [("a.json", 3), ("b.js", 2), ("c.txt", 1)]


def test_change_artifacts_tests_and_excluded_paths_are_not_scanned(tmp_path):
    for folder in (".prospec/changes/x", "prospec/specs", "tests"):
        write(tmp_path, f"{folder}/note.md", "繁體中文\n")
    write(tmp_path, "pkg/demo/a.txt", "中文\n")
    assert check_no_cjk.scan(tmp_path, exclude=["pkg/demo"]) == []
    assert len(check_no_cjk.scan(tmp_path)) == 1


def test_main_exit_code_reflects_hits(tmp_path, capsys):
    write(tmp_path, "ok.py", 'X = "fine"\n')
    assert check_no_cjk.main([str(tmp_path)]) == 0
    write(tmp_path, "bad.py", 'X = "中"\n')
    assert check_no_cjk.main([str(tmp_path)]) == 1
    assert "bad.py:1" in capsys.readouterr().out


def test_source_tree_has_no_cjk():
    hits = check_no_cjk.scan(ROOT / "src")
    assert hits == [], "\n".join(map(str, hits))


def test_index_html_declares_english():
    assert '<html lang="en">' in (ROOT / "src" / "decision_ledger" / "web" / "static" / "index.html").read_text(encoding="utf-8")


STATIC = ROOT / "src" / "decision_ledger" / "web" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("name", sorted(p.name for p in STATIC.glob("*.js")))
def test_static_js_modules_parse(name):
    """Regression pin for review R1-01: a translated string must never break module syntax (plain `node --check` misses it)."""
    source = (STATIC / name).read_text(encoding="utf-8")
    result = subprocess.run(["node", "--input-type=module", "--check"], input=source, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_cli_exclude_option(tmp_path, capsys):
    """Review R1-03: the CLI can reproduce the test's exclusion."""
    write(tmp_path, "pkg/demo/a.txt", "中文\n")
    assert check_no_cjk.main([str(tmp_path), "--exclude", "pkg/demo"]) == 0
    assert check_no_cjk.main([str(tmp_path)]) == 1


def test_wider_cjk_ranges_are_detected(tmp_path):
    """Review R1-06: compatibility ideographs, Bopomofo and Extension B are CJK too."""
    write(tmp_path, "a.txt", "豈\nㄅ\n\U00020000\nplain\n")
    assert lines(check_no_cjk.scan(tmp_path)) == [1, 2, 3]


def test_ui_labels_synthetic_data_and_recorded_run():
    """REQ-LEDGER-PLATFORM-006 AC3: the header always says Synthetic; the Fake backend reads "Recorded run"."""
    from tests.ui_text import shows

    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="synthetic"' in index and shows(STATIC / "index.html", "Synthetic data")
    assert shows(STATIC / "app.js", "Recorded run")  # DL-031: literal or its en.js key


ZH_DICT = "decision_ledger/web/static/locales/zh-TW.json"


def test_only_the_traditional_chinese_dictionary_is_allowed():
    """REQ-LEDGER-PLATFORM-028 (DL-031): one allowlisted file, and it really holds the Chinese strings."""
    assert set(check_no_cjk.ALLOWED_SUFFIXES) == {ZH_DICT}
    dictionary = ROOT / "src" / ZH_DICT
    assert check_no_cjk.file_hits(dictionary)  # the file itself is Chinese
    assert check_no_cjk.scan(ROOT / "src") == check_no_cjk.scan(ROOT / "src" / "decision_ledger") == []


def test_allowlist_matches_by_path_suffix_from_any_root(tmp_path):
    allowed = tmp_path / "x" / "decision_ledger" / "web" / "static" / "locales" / "zh-TW.json"
    write(tmp_path, str(allowed.relative_to(tmp_path)), '{"a": "中文"}\n')
    write(tmp_path, "x/decision_ledger/web/static/old/zh-TW.json", '{"a": "中文"}\n')
    write(tmp_path, "x/decision_ledger/web/static/other.json", '{"a": "中文"}\n')
    assert sorted(h.path.relative_to(tmp_path).as_posix() for h in check_no_cjk.scan(tmp_path)) == [
        "x/decision_ledger/web/static/old/zh-TW.json", "x/decision_ledger/web/static/other.json"]
