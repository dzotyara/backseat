"""Turning raw model output into something safe to post."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

TELEGRAM_LIMIT = 4000  # a bit under Telegram's 4096 to leave room for surrogate pairs

_ACTION_RE = re.compile(r"^\W*(SKIP|REPLY|REACT)\b(?:\W*?(\d+))?(.*)$", re.IGNORECASE)
_PROTOCOL_LINE_RE = re.compile(r"^\W*(SKIP|REPLY|REACT)\b", re.IGNORECASE)
_FENCE_LINE_RE = re.compile(r"^\s*```\w*\s*$")
_TRANSCRIPT_PREFIX_RE = re.compile(r"^#\d+\s+(?:\d{1,2}:\d{2}\s+)?")
_SPEAKER_PREFIX_RE = re.compile(r"^(?:Ты|Бот)\s*(?:\[\d+\])?\s*:\s*", re.IGNORECASE)
_QUOTE_PAIRS = {'""', "«»", "“”", "''"}


@dataclass(frozen=True, slots=True)
class Action:
    kind: str  # "skip" | "reply" | "react"
    message_id: int | None = None
    text: str = ""
    emoji: str = ""


SKIP = Action("skip")


def clean_reply(text: str) -> str:
    """Strip what models add around a chat message: fences, an echoed REPLY line,
    transcript prefixes like "#123 12:34 Ты:", wrapping quotes."""
    lines = text.strip().splitlines()
    while lines and _FENCE_LINE_RE.match(lines[0]):
        lines.pop(0)
    while lines and _FENCE_LINE_RE.match(lines[-1]):
        lines.pop()
    if len(lines) > 1 and _PROTOCOL_LINE_RE.match(lines[0]):
        lines.pop(0)
    text = "\n".join(lines).strip()
    text = _TRANSCRIPT_PREFIX_RE.sub("", text)
    text = _SPEAKER_PREFIX_RE.sub("", text)
    if len(text) >= 2 and text[0] + text[-1] in _QUOTE_PAIRS:
        text = text[1:-1].strip()
    return text.strip()


def parse_action(raw: str, valid_ids: Sequence[int], emojis: Sequence[str]) -> Action:
    """Parse the unprompted-comment protocol (SKIP / REPLY #id + text / REACT #id emoji).
    Anything unrecognisable is a SKIP: better silent than posting garbage."""
    if not valid_ids:
        return SKIP
    lines = raw.strip().splitlines()
    for index, line in enumerate(lines):
        match = _ACTION_RE.match(line.strip())
        if not match:
            continue
        kind = match.group(1).upper()
        if kind == "SKIP":
            return SKIP
        target = int(match.group(2)) if match.group(2) else None
        if target not in valid_ids:
            target = valid_ids[-1]
        rest = match.group(3).strip()
        if kind == "REACT":
            normalized = rest.replace("️", "")  # "❤️" -> "❤"
            emoji = next((e for e in emojis if e in normalized), None)
            return Action("react", target, emoji=emoji) if emoji else SKIP
        text = clean_reply("\n".join(lines[index + 1 :]) or rest)
        return Action("reply", target, text=text) if text else SKIP
    return SKIP


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split a long reply into Telegram-sized chunks, preferring line breaks."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text.strip():
        chunks.append(text)
    return chunks
