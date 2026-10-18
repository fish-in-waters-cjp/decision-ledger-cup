"""Does a web UI file show a sentence? Either as a literal or through a dictionary key (DL-031).

Lane B's UI tests asserted English sentences inside the JS files. After DL-031 the sentence lives in
web/static/locales/en.js and the JS refers to its key, so these tests ask "is it shown", not "where is it typed".
"""

import json
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "decision_ledger" / "web" / "static"
PREFIX = "export const EN = "


def english() -> dict[str, str]:
    return json.loads((STATIC / "locales" / "en.js").read_text(encoding="utf-8")[len(PREFIX):].rstrip().rstrip(";"))


def without_comments(text: str) -> str:
    """Drop // line comments and /* */ blocks so a key mentioned only in a comment does not count (review R1-08)."""
    return re.sub(r"/\*.*?\*/", "", re.sub(r"(?m)^\s*//.*$|\s//[^'\"`\n]*$", "", text), flags=re.S)


def shows(path: Path, sentence: str) -> bool:
    text = without_comments(path.read_text(encoding="utf-8"))
    if sentence in text:
        return True
    return any(f"'{key}'" in text or f'"{key}"' in text for key, value in english().items() if value == sentence)
