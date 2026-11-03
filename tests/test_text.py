from __future__ import annotations

from datetime import UTC, datetime, timedelta

from all_around_bot.render import _font
from all_around_bot.text import format_message_time, sanitize_content, wrap_text


def test_sanitize_replaces_discord_markup() -> None:
    raw = "hey <@123> and <@!456>, see <#789> <@&42> <:kek:555> <a:party:999>"
    assert (
        sanitize_content(raw) == "hey @user and @user, see #channel @role <:kek:555> <a:party:999>"
    )


def test_sanitize_renders_timestamp_tokens() -> None:
    assert sanitize_content("<t:1700000000:F>") == "2023-11-14 22:13 UTC"


def test_sanitize_strips_surrounding_whitespace() -> None:
    assert sanitize_content("  hello  ") == "hello"


def test_wrap_text_respects_width() -> None:
    font = _font(20)
    lines = wrap_text("lorem ipsum dolor " * 50, font, 300)
    assert lines
    assert all(font.getlength(line) <= 300 for line in lines)


def test_wrap_text_preserves_blank_lines() -> None:
    font = _font(20)
    assert wrap_text("a\n\nb", font, 300) == ["a", "", "b"]


def test_wrap_text_breaks_long_words() -> None:
    font = _font(20)
    lines = wrap_text("x" * 500, font, 100)
    assert len(lines) > 1
    assert all(font.getlength(line) <= 100 for line in lines)


def test_format_message_time_today() -> None:
    now = datetime.now(UTC)
    assert format_message_time(now, now=now).startswith("Today at ")


def test_format_message_time_yesterday() -> None:
    now = datetime.now(UTC)
    assert format_message_time(now - timedelta(days=1), now=now).startswith("Yesterday at ")


def test_format_message_time_older() -> None:
    now = datetime.now(UTC)
    result = format_message_time(now - timedelta(days=400), now=now)
    assert "at" not in result
