"""Render a stored message as a Discord-style screenshot card."""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .db import StoredMessage
from .emoji import image_for as emoji_image
from .text import Run, format_message_time, layout, sanitize_content

# Layout is expressed in "logical" pixels, then scaled up for a crisp result.
SCALE = 2
BASE_WIDTH = 620
PADDING = 24
AVATAR_SIZE = 40
GAP = 16
NAME_SIZE = 16
TIME_SIZE = 12
BODY_SIZE = 16
LINE_HEIGHT = 22
MAX_LINES = 40

# Twemoji are drawn slightly larger than body text, like in the Discord client.
EMOJI_SIZE = 20
EMOJI_GAP = 2

BACKGROUND = (49, 51, 56)  # #313338
TEXT_COLOR = (219, 222, 225)  # #DBDEE1
NAME_COLOR = (242, 243, 245)  # #F2F3F5
MUTED_COLOR = (148, 155, 164)  # #949BA4

# Deterministic avatar fallbacks for when the real avatar cannot be fetched.
AVATAR_COLORS = (
    (88, 101, 242),
    (59, 165, 93),
    (240, 178, 50),
    (235, 69, 158),
    (45, 164, 224),
    (155, 89, 182),
)

_REGULAR_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)

_BOLD_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
)


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load the best available font for the size, falling back to Pillow's default."""
    for candidate in _BOLD_FONTS if bold else _REGULAR_FONTS:
        path = Path(candidate)
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue

    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _circular_avatar(data: bytes, size: int) -> Image.Image:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image = ImageOps.fit(image, (size, size), Image.Resampling.LANCZOS)

    # Draw the mask oversized, then downscale, so the edges stay smooth.
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)

    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(image, (0, 0), mask)
    return result


def _fallback_avatar(name: str, user_id: int, size: int) -> Image.Image:
    color = AVATAR_COLORS[user_id % len(AVATAR_COLORS)]
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, size - 1, size - 1), fill=color)

    initial = (name.strip()[:1] or "?").upper()
    font = _font(int(size * 0.45), bold=True)
    draw.text((size / 2, size / 2), initial, font=font, fill=(255, 255, 255), anchor="mm")
    return image


def render_message_card(
    message: StoredMessage,
    *,
    avatar: bytes | None = None,
    width: int = BASE_WIDTH,
    emoji_dir: Path | str = "data/emoji",
    now=None,
) -> bytes:
    """Return a PNG of ``message`` laid out like a Discord message."""
    scale = SCALE
    pad = PADDING * scale
    avatar_size = AVATAR_SIZE * scale
    gap = GAP * scale
    text_x = pad + avatar_size + gap
    text_width = width * scale - text_x - pad

    name_font = _font(NAME_SIZE * scale, bold=True)
    time_font = _font(TIME_SIZE * scale)
    body_font = _font(BODY_SIZE * scale)
    line_height = LINE_HEIGHT * scale

    emoji_size = EMOJI_SIZE * scale
    emoji_advance = (EMOJI_SIZE + EMOJI_GAP) * scale

    body = sanitize_content(message.content) or "(no text)"
    lines = layout(body, body_font, text_width, emoji_advance=emoji_advance)
    if len(lines) > MAX_LINES:
        ellipsis = "…"
        lines = [
            *lines[:MAX_LINES],
            [Run("text", ellipsis, body_font.getlength(ellipsis))],
        ]

    name = message.author_name or "unknown"
    stamp = format_message_time(message.created_at, now=now)

    # Place the header and body relative to a shared baseline.
    baseline = pad + int(NAME_SIZE * scale * 0.85)
    header_height = (NAME_SIZE + 4) * scale
    ascent, descent = body_font.getmetrics()
    body_baseline = pad + header_height + ascent

    text_bottom = body_baseline + (len(lines) - 1) * line_height + descent
    content_bottom = max(pad + avatar_size, text_bottom + 4 * scale)
    height = content_bottom + pad

    image = Image.new("RGB", (width * scale, height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    if avatar:
        try:
            avatar_image = _circular_avatar(avatar, avatar_size)
        except OSError:
            avatar_image = _fallback_avatar(name, message.author_id, avatar_size)
    else:
        avatar_image = _fallback_avatar(name, message.author_id, avatar_size)
    image.paste(avatar_image, (pad, pad), avatar_image)

    draw.text((text_x, baseline), name, font=name_font, fill=NAME_COLOR, anchor="ls")
    name_width = name_font.getlength(name)
    draw.text(
        (text_x + name_width + 8 * scale, baseline),
        stamp,
        font=time_font,
        fill=MUTED_COLOR,
        anchor="ls",
    )

    emoji_top = -emoji_size + descent
    for index, line in enumerate(lines):
        x = float(text_x)
        y = body_baseline + index * line_height

        for run in line:
            if run.kind == "emoji":
                emoji_png = emoji_image(run.value, emoji_size, emoji_dir)
                if emoji_png is not None:
                    image.paste(emoji_png, (int(x), int(y + emoji_top)), emoji_png)
                else:
                    label = run.label or run.value
                    draw.text((x, y), label, font=body_font, fill=TEXT_COLOR, anchor="ls")
                x += run.width
            else:
                draw.text((x, y), run.value, font=body_font, fill=TEXT_COLOR, anchor="ls")
                x += run.width

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
