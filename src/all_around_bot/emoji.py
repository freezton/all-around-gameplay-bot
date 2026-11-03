"""Emoji assets for rendering inline emoji in message cards.

Unicode emoji use Twemoji (what the Discord client itself renders); custom
server emoji are fetched from Discord's CDN by ID. Everything is cached on disk
after the first download.
"""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

TWEMOJI_VERSION = "15.1.0"
TWEMOJI_URL = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@{version}/assets/72x72/{name}.png"
DISCORD_EMOJI_URL = "https://cdn.discordapp.com/emojis/{id}.png?size=64"
DOWNLOAD_TIMEOUT = 5.0
# Discord's CDN rejects the default `Python-urllib` user agent with a 403.
USER_AGENT = "DiscordBot (all-around-bot, 0.1)"

DISCORD_PREFIX = "discord:"

_cache: dict[tuple[str, int, str], Image.Image | None] = {}


def asset_name(char: str) -> str:
    """Twemoji filenames are lowercase codepoints, without the FE0F selector."""
    return "-".join(f"{ord(c):x}" for c in char if c != "\ufe0f")


def image_for(ref: str, size: int, cache_dir: Path | str = "data/emoji") -> Image.Image | None:
    """Return an emoji as a square RGBA image, downloading it if needed.

    ``ref`` is either a Unicode emoji or ``discord:<emoji id>``.
    """
    key = (ref, size, str(cache_dir))
    if key in _cache:
        return _cache[key]

    if ref.startswith(DISCORD_PREFIX):
        emoji_id = ref.removeprefix(DISCORD_PREFIX)
        path = Path(cache_dir) / f"discord-{emoji_id}.png"
        url = DISCORD_EMOJI_URL.format(id=emoji_id)
    else:
        path = Path(cache_dir) / f"{asset_name(ref)}.png"
        url = TWEMOJI_URL.format(version=TWEMOJI_VERSION, name=asset_name(ref))

    if not path.exists():
        path = _download(url, path, ref)

    image: Image.Image | None = None
    if path is not None:
        try:
            with Image.open(path) as raw:
                image = raw.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
        except OSError:
            log.warning("Unreadable emoji asset: %s", path)

    _cache[key] = image
    return image


def _download(url: str, path: Path, ref: str) -> Path | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            data = response.read()
    except OSError:
        log.warning("Could not download emoji %s (%s)", ref, url)
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(path)
    return path
