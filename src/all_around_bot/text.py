"""Text helpers: Discord markup cleanup, emoji-aware layout, timestamps."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import emoji as _emoji

_CUSTOM_EMOJI = re.compile(r"<a?:([A-Za-z0-9_~]+):(\d+)>")
_USER_MENTION = re.compile(r"<@!?\d+>")
_ROLE_MENTION = re.compile(r"<@&\d+>")
_CHANNEL_MENTION = re.compile(r"<#\d+>")
_TIMESTAMP = re.compile(r"<t:(\d+)(?::[tTdDfFR])?>")

Kind = Literal["text", "emoji", "space"]


@dataclass(frozen=True, slots=True)
class Run:
    """A piece of a laid-out line: a word, a space, or a single emoji."""

    kind: Kind
    value: str
    width: float
    label: str | None = None


@dataclass(frozen=True, slots=True)
class EmojiToken:
    """An emoji found in the source text.

    ``value`` is either the Unicode emoji itself or ``discord:<id>`` for a
    custom server emoji; ``label`` is the text used if the image is unavailable.
    """

    value: str
    label: str


def sanitize_content(text: str) -> str:
    """Turn raw Discord markup into something readable in a rendered image."""

    def _timestamp(match: re.Match[str]) -> str:
        moment = datetime.fromtimestamp(int(match.group(1)), tz=UTC)
        return moment.strftime("%Y-%m-%d %H:%M UTC")

    text = _TIMESTAMP.sub(_timestamp, text)
    text = _USER_MENTION.sub("@user", text)
    text = _ROLE_MENTION.sub("@role", text)
    text = _CHANNEL_MENTION.sub("#channel", text)
    return text.strip()


def split_runs(text: str) -> list[str | EmojiToken]:
    """Split text into plain-text segments and emoji tokens.

    Custom emoji (``<:name:id>``) are kept as tokens so the renderer can fetch
    the real image from Discord's CDN.
    """
    spans: list[tuple[int, int, EmojiToken]] = []
    for item in _emoji.emoji_list(text):
        token = EmojiToken(item["emoji"], item["emoji"])
        spans.append((item["match_start"], item["match_end"], token))

    for match in _CUSTOM_EMOJI.finditer(text):
        token = EmojiToken(f"discord:{match.group(2)}", f":{match.group(1)}:")
        spans.append((match.start(), match.end(), token))

    spans.sort(key=lambda span: span[0])

    pieces: list[str | EmojiToken] = []
    cursor = 0
    for start, end, token in spans:
        if start < cursor:  # overlapping match, ignore it
            continue
        if start > cursor:
            pieces.append(text[cursor:start])
        pieces.append(token)
        cursor = end

    if cursor < len(text):
        pieces.append(text[cursor:])
    return pieces


def _cut_index(word: str, font, max_width: float) -> int:
    """Longest prefix of ``word`` that still fits, always at least one character."""
    low, high = 1, len(word)
    while low < high:
        mid = (low + high + 1) // 2
        if font.getlength(word[:mid]) <= max_width:
            low = mid
        else:
            high = mid - 1
    return low


def _hard_split(word: str, font, max_width: float) -> list[str]:
    chunks: list[str] = []
    while len(word) > 1 and font.getlength(word) > max_width:
        cut = _cut_index(word, font, max_width)
        chunks.append(word[:cut])
        word = word[cut:]
    chunks.append(word)
    return chunks


def _tokens(paragraph: str, font, emoji_advance: float) -> list[Run]:
    tokens: list[Run] = []
    for segment in split_runs(paragraph):
        if isinstance(segment, EmojiToken):
            tokens.append(Run("emoji", segment.value, emoji_advance, segment.label))
            continue

        for piece in re.split(r"(\s+)", segment):
            if not piece:
                continue
            if piece.isspace():
                tokens.append(Run("space", " ", font.getlength(piece)))
            else:
                tokens.append(Run("text", piece, font.getlength(piece)))
    return tokens


def layout(
    text: str,
    font,
    max_width: float,
    *,
    emoji_advance: float | None = None,
) -> list[list[Run]]:
    """Lay out ``text`` into lines of runs no wider than ``max_width``."""
    advance = emoji_advance if emoji_advance is not None else float(font.size)

    lines: list[list[Run]] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append([])
            continue

        line: list[Run] = []
        width = 0.0
        space: Run | None = None

        for token in _tokens(paragraph, font, advance):
            if token.kind == "space":
                if line:
                    space = token
                continue

            if token.width > max_width:
                if line:
                    lines.append(line)
                    line = []
                    width = 0.0

                chunks = _hard_split(token.value, font, max_width)
                for chunk in chunks[:-1]:
                    lines.append([Run("text", chunk, font.getlength(chunk))])

                tail = chunks[-1]
                line = [Run("text", tail, font.getlength(tail))]
                width = font.getlength(tail)
                space = None
                continue

            extra = space.width if space else 0.0
            if line and width + extra + token.width > max_width:
                lines.append(line)
                line = []
                width = 0.0
                space = None

            if space:
                line.append(space)
                width += space.width
                space = None

            line.append(token)
            width += token.width

        lines.append(line)

    return lines


def wrap_text(
    text: str,
    font,
    max_width: float,
    *,
    emoji_advance: float | None = None,
) -> list[str]:
    """Plain-string variant of :func:`layout`, handy for tests and diagnostics."""
    return [
        "".join(run.label or run.value for run in line)
        for line in layout(text, font, max_width, emoji_advance=emoji_advance)
    ]


def format_message_time(value: datetime, *, now: datetime | None = None) -> str:
    """Format a timestamp the way Discord does in message headers."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    local = value.astimezone()
    reference = (now or datetime.now(UTC)).astimezone(local.tzinfo)

    clock = local.strftime("%I:%M %p").lstrip("0")
    day_delta = (reference.date() - local.date()).days

    if day_delta == 0:
        return f"Today at {clock}"
    if day_delta == 1:
        return f"Yesterday at {clock}"
    if local.year == reference.year:
        return f"{local.strftime('%B')} {local.day} at {clock}"
    return f"{local.strftime('%B')} {local.day}, {local.year}"
