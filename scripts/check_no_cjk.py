"""Fail when judge-facing text contains CJK characters.

What counts as judge-facing (and therefore scanned):

* Python: string constants, read from the *source text* of each ``ast.Constant``
  node, so comments, docstrings and ``\\uXXXX`` escapes never match. Functional
  CJK handling (parsers that accept a full-width colon, and so on) is written
  with escapes on purpose.
* HTML: text nodes and attribute values; comments are skipped.
* JSON: every string value and key.
* Everything else we ship (JS, CSS, TXT, diffs, Markdown): the whole file.

Usage: ``uv run python scripts/check_no_cjk.py [ROOT ...] [--exclude REL_PATH ...]`` (default ``src``).
The exit code is the number of hits, capped at 1.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

# CJK symbols and punctuation, kana, Bopomofo, enclosed/compatibility CJK, unified
# ideographs (incl. extensions A to H), Hangul syllables, compatibility ideographs,
# vertical/compatibility forms and full-width forms.
CJK = re.compile(
    r"[\u3000-\u303f\u3040-\u30ff\u3100-\u312f\u3200-\u33ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef\U00020000-\U0003134f]"
)

TEXT_SUFFIXES = {".js", ".css", ".txt", ".diff", ".md", ".svg", ".yaml", ".yml", ".toml"}
SKIP_DIRS = {".prospec", "prospec", "tests", "__pycache__", ".git", ".venv", "node_modules"}
DEFAULT_EXCLUDES: tuple[str, ...] = ()
# The only files allowed to hold CJK text, matched by path suffix so any scan root agrees.
ALLOWED_SUFFIXES: dict[str, str] = {
    "decision_ledger/web/static/locales/zh-TW.json": "the Traditional Chinese interface dictionary (DL-031); shown only after a user switches language",
}


def is_allowed(path: Path) -> bool:
    posix = path.as_posix()
    return any(posix == suffix or posix.endswith("/" + suffix) for suffix in ALLOWED_SUFFIXES)


@dataclass(frozen=True)
class Hit:
    path: Path
    line: int
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.text.strip()[:120]}"


def _line_hits(path: Path, text: str, first_line: int = 1) -> Iterator[Hit]:
    for offset, line in enumerate(text.splitlines()):
        if CJK.search(line):
            yield Hit(path, first_line + offset, line)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def python_hits(path: Path, source: str) -> list[Hit]:
    """String constants whose source text contains CJK (docstrings and comments excluded)."""
    tree = ast.parse(source, filename=str(path))
    docstrings = _docstring_nodes(tree)
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            segment = ast.get_source_segment(source, node) or ""
            hits.extend(_line_hits(path, segment, node.lineno))
    return sorted(set(hits), key=lambda h: h.line)


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]
        self.chunks.extend((line, value) for _, value in attrs if value)

    handle_startendtag = handle_starttag

    def handle_data(self, data: str) -> None:
        self.chunks.append((self.getpos()[0], data))


def html_hits(path: Path, source: str) -> list[Hit]:
    """Text nodes and attribute values; HTML comments are not reported."""
    parser = _HtmlText()
    parser.feed(source)
    parser.close()
    return [hit for line, chunk in parser.chunks for hit in _line_hits(path, chunk, line)]


def _json_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def json_hits(path: Path, source: str) -> list[Hit]:
    """Every string key and value; line numbers point at the first line that contains the string."""
    lines = source.splitlines()
    hits: list[Hit] = []
    for text in _json_strings(json.loads(source)):
        if CJK.search(text):
            probe = json.dumps(text, ensure_ascii=False)[1:-1][:20]
            line = next((i for i, raw in enumerate(lines, 1) if probe in raw), 1)
            hits.append(Hit(path, line, text))
    return hits


def file_hits(path: Path) -> list[Hit]:
    """Dispatch on the file suffix; unknown binary-ish suffixes are ignored."""
    suffix = path.suffix.lower()
    if suffix not in TEXT_SUFFIXES | {".py", ".html", ".htm", ".json"}:
        return []
    source = path.read_text(encoding="utf-8")
    if suffix == ".py":
        return python_hits(path, source)
    if suffix in {".html", ".htm"}:
        return html_hits(path, source)
    if suffix == ".json":
        return json_hits(path, source)
    return list(_line_hits(path, source))


def iter_files(root: Path, exclude: Iterable[str] = DEFAULT_EXCLUDES) -> Iterator[Path]:
    excluded = [root / e if not Path(e).is_absolute() else Path(e) for e in exclude]
    candidates = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    for path in candidates:
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]) if path != root else False:
            continue
        if any(path == e or e in path.parents for e in excluded):
            continue
        if is_allowed(path.resolve()):
            continue
        yield path


def scan(root: str | Path, exclude: Iterable[str] = DEFAULT_EXCLUDES) -> list[Hit]:
    """Return every CJK hit under ``root`` (a file or directory), skipping ``exclude`` paths relative to it."""
    root = Path(root)
    return [hit for path in iter_files(root, exclude) for hit in file_hits(path)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report CJK characters in judge-facing text.")
    parser.add_argument("roots", nargs="*", default=["src"], help="files or directories to scan (default: src)")
    parser.add_argument("--exclude", action="append", default=[], help="path relative to each root to skip (repeatable)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    roots = args.roots
    hits = [hit for root in roots for hit in scan(root, exclude=args.exclude)]
    for hit in hits:
        print(hit)
    print(f"{len(hits)} CJK hit(s) in {', '.join(roots)}")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
