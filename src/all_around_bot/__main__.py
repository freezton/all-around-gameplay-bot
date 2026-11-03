"""Entry point: ``python -m all_around_bot`` or the ``all-around-bot`` script."""

from __future__ import annotations

import logging
import sys

from discord.errors import PrivilegedIntentsRequired

from .bot import AllAroundBot
from .config import get_settings

log = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    bot = AllAroundBot(settings)

    try:
        bot.run(settings.discord_token.get_secret_value(), log_handler=None)
    except PrivilegedIntentsRequired:
        log.critical(
            "Discord rejected the requested gateway intents.\n"
            "Fix: https://discord.com/developers/applications -> your app -> Bot ->\n"
            "Privileged Gateway Intents -> enable 'Message Content Intent', then restart.\n"
            "No re-invite is needed; intents are gateway settings, not OAuth2 scopes."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
