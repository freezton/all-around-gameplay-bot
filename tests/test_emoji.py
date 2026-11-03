from __future__ import annotations

import io
from datetime import UTC, datetime

from PIL import Image

from all_around_bot.db import StoredMessage
from all_around_bot.emoji import asset_name
from all_around_bot.render import _font, render_message_card
from all_around_bot.text import EmojiToken, layout, split_runs


def test_asset_name_strips_variation_selector() -> None:
    assert asset_name("❤️") == "2764"
    assert asset_name("😀") == "1f600"
    assert asset_name("👍🏽") == "1f44d-1f3fd"
    assert asset_name("👨‍👩‍👧") == "1f468-200d-1f469-200d-1f467"


def test_split_runs_separates_emoji() -> None:
    runs = split_runs("hi 😀 there ❤️")
    assert runs == [
        "hi ",
        EmojiToken("😀", "😀"),
        " there ",
        EmojiToken("❤️", "❤️"),
    ]


def test_split_runs_extracts_custom_emoji() -> None:
    runs = split_runs("nice <:kek:123> and <a:party:456>")
    assert runs == [
        "nice ",
        EmojiToken("discord:123", ":kek:"),
        " and ",
        EmojiToken("discord:456", ":party:"),
    ]


def test_layout_measures_emoji_as_advance() -> None:
    font = _font(20)
    lines = layout("aaaa 😀 bbbb", font, 1000, emoji_advance=30)
    assert len(lines) == 1

    runs = lines[0]
    emoji_runs = [run for run in runs if run.kind == "emoji"]
    assert len(emoji_runs) == 1
    assert emoji_runs[0].width == 30


def test_layout_wraps_around_emoji() -> None:
    font = _font(20)
    lines = layout("😀 😀 😀 😀 😀 😀", font, 60, emoji_advance=25)
    assert len(lines) > 1
    for line in lines:
        assert sum(run.width for run in line) <= 60


def _message(content: str) -> StoredMessage:
    return StoredMessage(
        message_id=1,
        guild_id=1,
        channel_id=2,
        author_id=7,
        author_name="tester",
        content=content,
        created_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
    )


def _seed_asset(directory, char: str, color=(255, 0, 255)) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (72, 72), (*color, 255)).save(directory / f"{asset_name(char)}.png")


def test_render_draws_cached_emoji_asset(tmp_path) -> None:
    cache_dir = tmp_path / "emoji"
    _seed_asset(cache_dir, "😀")

    png = render_message_card(_message("hello 😀"), emoji_dir=cache_dir)
    image = Image.open(io.BytesIO(png)).convert("RGB")

    colors = image.getcolors(maxcolors=1_000_000) or []
    assert any(color == (255, 0, 255) for _, color in colors)


def test_render_draws_cached_discord_emoji(tmp_path) -> None:
    cache_dir = tmp_path / "emoji"
    cache_dir.mkdir(parents=True)
    Image.new("RGBA", (64, 64), (0, 255, 0, 255)).save(cache_dir / "discord-777.png")

    png = render_message_card(_message("hey <:kek:777>"), emoji_dir=cache_dir)
    image = Image.open(io.BytesIO(png)).convert("RGB")

    colors = image.getcolors(maxcolors=1_000_000) or []
    assert any(color == (0, 255, 0) for _, color in colors)


def test_render_falls_back_when_asset_unavailable(tmp_path, monkeypatch) -> None:
    import all_around_bot.emoji as emoji_module

    monkeypatch.setattr(emoji_module, "_download", lambda url, path, ref: None)

    # A different emoji than any other test, so the module cache cannot interfere.
    png = render_message_card(_message("fallback 🦄"), emoji_dir=tmp_path / "empty")
    assert Image.open(io.BytesIO(png)).height > 0


def test_download_sends_user_agent(tmp_path, monkeypatch) -> None:
    import urllib.request

    import all_around_bot.emoji as emoji_module

    captured: dict[str, str] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return b"fake"

    def fake_urlopen(request, timeout=None):
        captured.update({k.lower(): v for k, v in dict(request.header_items()).items()})
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    target = tmp_path / "asset.png"
    assert emoji_module._download("https://example.test/a.png", target, "😀") == target
    assert captured.get("user-agent")
    assert target.read_bytes() == b"fake"
