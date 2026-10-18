"""Parse source files into addressable segments (pure functions, no I/O)."""

from __future__ import annotations

import hashlib
import json
import re

from decision_ledger.domain.errors import ParseError
from decision_ledger.domain.models import Segment, SourceKind

_LINE = re.compile(r"^\[(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<who>[^:\uff1a]+?)\s*[:\uff1a]\s*(?P<text>.*)$")


def content_hash(raw_text: str) -> str:
    """Return the sha256 of the source content, ignoring leading/trailing whitespace."""
    return hashlib.sha256(raw_text.strip().encode("utf-8")).hexdigest()


def parse_transcript(raw_text: str) -> list[Segment]:
    """Parse ``[hh:mm:ss] Speaker: text`` lines; indented or unmarked lines continue the previous one."""
    segments: list[Segment] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _LINE.match(line)
        if match:
            segments.append(
                Segment(index=len(segments), locator=match["ts"], speaker=match["who"].strip(), text=match["text"].strip())
            )
        elif segments:
            last = segments[-1]
            segments[-1] = last.model_copy(update={"text": f"{last.text} {line}".strip()})
        else:
            raise ParseError(f"transcript line has no [timestamp] Speaker: prefix: {line[:40]}")
    if not segments:
        raise ParseError("transcript has no lines")
    return segments


def parse_slack(raw_text: str) -> list[Segment]:
    """Parse a Slack channel export (a list of messages, or ``{"messages": [...]}``)."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"Slack export is not valid JSON: {exc.msg}") from exc
    messages = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(messages, list) or not messages:
        raise ParseError("Slack export has no messages")

    segments: list[Segment] = []
    for message in messages:
        if not isinstance(message, dict) or "ts" not in message:
            raise ParseError("Slack message is missing ts")
        text = str(message.get("text") or "").strip()
        if message.get("subtype") or not text:
            continue
        # Exports vary between tools: coerce field types instead of trusting them.
        profile = message.get("user_profile")
        real_name = profile.get("real_name") if isinstance(profile, dict) else None
        speaker = real_name or message.get("user_name") or message.get("user") or "unknown"
        segments.append(Segment(index=len(segments), locator=str(message["ts"]), speaker=str(speaker), text=text))
    if not segments:
        raise ParseError("Slack export has no text messages")
    return segments


def parse_source(kind: SourceKind, raw_text: str) -> list[Segment]:
    """Parse ``raw_text`` with the parser for ``kind``."""
    if kind is SourceKind.TRANSCRIPT:
        return parse_transcript(raw_text)
    return parse_slack(raw_text)
