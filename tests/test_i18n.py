"""DL-031: interface language switching (REQ-LEDGER-PLATFORM-040).

Dictionary checks run without Node: en.js is fixed to `export const EN = <JSON>;` so it parses as JSON.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "decision_ledger" / "web" / "static"
EN_JS = STATIC / "locales" / "en.js"
ZH_JSON = STATIC / "locales" / "zh-TW.json"
PREFIX = "export const EN = "

# Characters that only exist in Simplified Chinese (their Traditional forms differ).
SIMPLIFIED = set("这们时说对发会个么设录务实关与检应页题评审决证选项问显错误请组织网络开复缓数据认读写载览动态测试链节点规则历况级换输码删编辑统计际线处闭帮达过还进运连报导无专业")  # 理, 里 are also Traditional

# Tokens that may appear in JS string literals without translation: protocol words, HTTP, product names.
ALLOWED_WORDS = {"PR", "PRs", "HEAD", "HTTP", "JSON", "API", "GitHub", "Gemini", "Slack", "Decision", "Ledger", "LLM", "OK", "ID", "URL", "zh", "Hant",
                 "Escape"}  # KeyboardEvent.key value compared in app.js, not shown


def english() -> dict[str, str]:
    text = EN_JS.read_text(encoding="utf-8")
    assert text.startswith(PREFIX) and text.rstrip().endswith(";"), "en.js must be `export const EN = <JSON>;`"
    return json.loads(text[len(PREFIX):].rstrip().rstrip(";"))


def chinese() -> dict[str, str]:
    return json.loads(ZH_JSON.read_text(encoding="utf-8"))


def js_files() -> list[Path]:
    return sorted(p for p in STATIC.glob("*.js"))


def used_keys() -> set[str]:
    keys: set[str] = set()
    sources = [p.read_text(encoding="utf-8") for p in js_files()] + [(STATIC / "index.html").read_text(encoding="utf-8")]
    for text in sources:
        keys |= set(re.findall(r"\bt\(\s*'([a-z0-9_.-]+)'", text))
        keys |= {k + ".other" for k in re.findall(r"\btn\(\s*'([a-z0-9_.-]+)'", text)}
        keys |= set(re.findall(r'data-i18n(?:-title|-aria-label)?="([a-z0-9_.-]+)"', text))
    return keys


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_i18n_node_tests():
    run = subprocess.run(["node", "--test", "tests/web/i18n.test.mjs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr


def key_like_literals() -> set[str]:
    """String literals shaped like a dictionary key in a namespace en.js uses (catches keys kept in arrays and maps)."""
    namespaces = {k.split(".")[0] for k in english()}
    found = set()
    for path in js_files():
        for literal in string_literals(path.read_text(encoding="utf-8")):
            if re.fullmatch(r"[a-z]+(?:\.[a-z0-9_-]+)+", literal) and literal.split(".")[0] in namespaces and not literal.endswith(".js"):
                found.add(literal)
    return found


def test_every_key_used_by_the_ui_exists_in_english():
    en = set(english())
    plural_bases = {k.rsplit(".", 1)[0] for k in en if k.endswith((".one", ".other"))}
    missing = sorted(used_keys() - en)
    assert used_keys() and missing == []
    dangling = sorted(k for k in key_like_literals() if k not in en and k not in plural_bases)
    assert dangling == []


def test_both_dictionaries_have_the_same_keys():
    en = {k for k in english() if not k.endswith(".one")}
    zh = set(chinese())
    assert not any(k.endswith(".one") for k in zh)
    assert sorted(en - zh) == [] and sorted(zh - en) == []


def test_chinese_dictionary_is_traditional_and_complete():
    zh = chinese()
    bad = {k: v for k, v in zh.items() if SIMPLIFIED & set(v)}
    assert bad == {}
    assert all(v.strip() for v in zh.values())
    en = english()
    for key, value in zh.items():  # the same placeholders in both languages, in both directions
        english_value = en[key]
        assert set(re.findall(r"\{(\w+)\}", value)) == set(re.findall(r"\{(\w+)\}", english_value)), key


def test_every_backend_problem_type_has_a_title_key():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "decision_ledger" / "api").glob("*.py"))
    slugs = set(re.findall(r'\b\d{3}, "([a-z][a-z-]+)", "', src))
    assert slugs and sorted(f"problem.{s}" for s in slugs if f"problem.{s}" not in english()) == []


# ---- structural check: no English interface text left in JS string literals ----

def string_literals(source: str) -> list[str]:
    """Static text of every JS string and template literal (template ${...} parts dropped); comments skipped."""
    out, i, n = [], 0, len(source)
    while i < n:
        c = source[i]
        if source.startswith("//", i):
            i = source.find("\n", i)
            i = n if i < 0 else i
        elif source.startswith("/*", i):
            i = source.find("*/", i + 2) + 2
        elif c in "'\"":
            j, buf = i + 1, []
            while j < n and source[j] != c:
                if source[j] == "\\":
                    buf.append(source[j + 1] if j + 1 < n else "")
                    j += 2
                    continue
                buf.append(source[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
        elif c == "`":
            j, buf, depth = i + 1, [], 0
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if depth == 0 and source[j] == "`":
                    break
                if source.startswith("${", j):
                    depth += 1
                    buf.append(" ")
                    j += 2
                    continue
                if depth and source[j] == "{":
                    depth += 1
                elif depth and source[j] == "}":
                    depth -= 1
                    j += 1
                    continue
                if depth == 0:
                    buf.append(source[j])
                elif source[j] in "'\"`":  # nested literals inside ${...} are collected recursively below
                    pass
                j += 1
            out.append("".join(buf))
            inner = source[i + 1:j]
            out.extend(string_literals("".join(re.findall(r"\$\{((?:[^{}]|\{[^{}]*\})*)\}", inner))))
            i = j + 1
        else:
            i += 1
    return out


def visible_text(literal: str) -> list[str]:
    """Text a person would read: HTML text nodes plus title, aria-label, placeholder and alt values."""
    attrs = re.findall(r'(?:title|aria-label|placeholder|alt)="([^"]*)"', literal)
    text = re.sub(r"<[^>]*>", " ", literal)
    return attrs + [text]


def english_words(text: str, keys: set[str]) -> list[str]:
    if text.strip() in keys:
        return []
    words = re.findall(r"[A-Za-z][A-Za-z']+", text)
    # A UI phrase has a capitalised word or two plain words in a row; identifiers, classes and paths do not.
    looks_like_ui = re.search(r"\b[A-Z][a-z]+\b", text) or re.search(r"\b[a-z]{2,} [a-z]{2,}\b", text)
    if not looks_like_ui or re.fullmatch(r"[\w\-./:#?=&%*, ]*", text) and not re.search(r"[A-Z][a-z]", text) and " " not in text.strip():
        return []
    return [w for w in words if w not in ALLOWED_WORDS]


# Literals that look like phrases but are not interface text (CSS classes, data attributes, MIME types).
# Attribute lists such as `data-merge-toggle aria-expanded="${...}"` count too; title, aria-label,
# placeholder and alt values inside them are still checked on their own by visible_text().
# pages.js DEFAULT_QUESTIONS are data: recorded answers are keyed by the question text, so they stay English.
RECORDED_QUESTIONS = (
    "Why did the beta launch move?",
    "How did the beta launch date change over time?",
    "Why can't payments code use the database client directly?",
    "Is guest checkout allowed in the beta?",
)
NOT_UI = re.compile(r'^(?:[a-z0-9-]+(?:="[^"]*")?(?: [a-z0-9-]+(?:="[^"]*")?)*|application/\w+|text/\w+|[a-z]+/[a-z0-9-]+|'
                    + "|".join(map(re.escape, RECORDED_QUESTIONS)) + r')$')


def markup_text(literal: str) -> list[str]:
    """In a literal that holds markup, every text node and title/aria-label/placeholder/alt value is interface text,
    whatever its case (review R1-02: lower-case words such as "bot" or "loading" slipped through)."""
    if "<" not in literal or ">" not in literal:
        return []
    parts = re.findall(r'(?:title|aria-label|placeholder|alt)="([^"]*)"', literal)
    parts += re.findall(r">([^<>]*)<", literal)
    return [p for p in (" ".join(x.split()) for x in parts) if re.search(r"[A-Za-z]{2,}", p) and not set(re.findall(r"[A-Za-z]{2,}", p)) <= ALLOWED_WORDS]


# Code sinks that put a plain string literal on screen (review R1-02).
SINKS = [
    r"\.textContent\s*=\s*(['\"`])([^'\"`$]*[A-Za-z]{2,}[^'\"`]*)\1",
    r"\b(?:alert|confirm)\(\s*(['\"`])([^'\"`$]*[A-Za-z]{2,}[^'\"`]*)\1",
    r"\bbutton\(\s*(['\"`])([^'\"`$]*[A-Za-z]{2,}[^'\"`]*)\1",
    r"\?\s*(['\"])([a-z][a-z ]*?)\1\s*:\s*['\"]\2s['\"]",  # n === 1 ? 'source' : 'sources'
]
# Known gap: a quoted word returned from a ${...} ternary inside markup (e.g. `: 'diff'}`) is not detected; a regex
# cannot tell it from comparisons such as kind === 'slack'. Screenshot review covers it (DL-031 review R1-02/R1-05).

# Markup text that is an identity, not wording: the bot's avatar initials and its account name.
ALLOWED_MARKUP = {"DL", "decision-ledger"}
# Protocol text that is sent, never shown: the HTTP Authorization scheme for the Firebase ID token (DL-032).
PROTOCOL_TEXT = {"Bearer"}


def untranslated(path: Path) -> list[str]:
    keys = set(english())
    source = path.read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", re.sub(r"(?m)^\s*//.*$", "", source), flags=re.S)
    found = []
    for literal in string_literals(source):
        for text in visible_text(literal):
            stripped = " ".join(text.split())
            if not stripped or NOT_UI.fullmatch(stripped) or stripped in PROTOCOL_TEXT:
                continue
            if english_words(stripped, keys):
                found.append(stripped[:80])
        found += [f"markup: {x[:70]}" for x in markup_text(literal) if x not in keys and x not in ALLOWED_MARKUP]
    for pattern in SINKS:
        found += [f"sink: {m.group(2)[:70]}" for m in re.finditer(pattern, code) if m.group(2) not in keys]
    return found


# Files whose strings are not moved to the dictionaries yet; each migration commit removes one, and the
# last one empties the set. strict=True turns a finished file red until it is removed from here.
PENDING_MIGRATION: set[str] = set()


@pytest.mark.parametrize("name", [
    pytest.param(p.name, marks=pytest.mark.xfail(strict=True, reason="strings not migrated yet (DL-031)"))
    if p.name in PENDING_MIGRATION else p.name
    for p in sorted(STATIC.glob("*.js"))
])
def test_js_has_no_untranslated_interface_text(name):
    assert untranslated(STATIC / name) == []


def test_structural_check_catches_literal_ui_text(tmp_path):
    probe = tmp_path / "probe.js"
    probe.write_text("const a = `<p class=\"empty\">No decisions yet</p>`; el.textContent = 'Connecting…'; button('Save changes', 'x');\n", encoding="utf-8")
    assert len(untranslated(probe)) >= 3
    lower = ("const b = `<span class=\"muted\">bot</span>`; el.textContent = 'loading'; "
             "const c = `<i title=\"reset everything\"></i>`; const d = n === 1 ? 'source' : 'sources'; alert('done');\n")
    probe.write_text(lower, encoding="utf-8")
    flagged = " | ".join(untranslated(probe))
    for word in ("bot", "loading", "reset everything", "source", "done"):
        assert word in flagged, word
    probe.write_text("const a = `<p class=\"empty\">${t('overview.empty')}</p>`; el.className = 'btn primary small';\n", encoding="utf-8")
    assert untranslated(probe) == []


def test_css_holds_no_interface_words():
    """CSS `content:` text cannot be translated; labels come from attributes set by applyStatic."""
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert re.findall(r"content:\s*['\"][^'\"]*[A-Za-z]{2,}", css) == []
