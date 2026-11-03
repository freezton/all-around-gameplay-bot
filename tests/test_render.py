from __future__ import annotations

import io
from datetime import UTC, datetime

from PIL import Image

from all_around_bot.db import StoredMessage
from all_around_bot.render import BASE_WIDTH, SCALE, render_message_card


def _message(content: str = "hello there") -> StoredMessage:
    return StoredMessage(
        message_id=1,
        guild_id=1,
        channel_id=2,
        author_id=7,
        author_name="tester",
        content=content,
        created_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
    )


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


def test_renders_png_at_scaled_width() -> None:
    image = _open(render_message_card(_message()))
    assert image.format == "PNG"
    assert image.width == BASE_WIDTH * SCALE
    assert image.height > 0


def test_long_message_is_truncated() -> None:
    short = _open(render_message_card(_message("hi")))
    long = _open(render_message_card(_message("word " * 2000)))
    assert long.height > short.height
    assert long.height < 20_000


def test_custom_width() -> None:
    image = _open(render_message_card(_message(), width=300))
    assert image.width == 300 * SCALE


def test_empty_content_still_renders() -> None:
    image = _open(render_message_card(_message("")))
    assert image.height > 0
