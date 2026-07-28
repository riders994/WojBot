"""Entry point: ``python -m wojbot`` (or the ``wojbot`` console script)."""

from __future__ import annotations

import sys

from .bot import WojBotV2
from .core.config import Settings
from .core.errors import WojBotException
from .core.logging import configure_logging


def main() -> None:
    try:
        settings = Settings.load()
    except WojBotException as exc:
        # Fail with a clear message instead of a traceback for config problems.
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    handler = configure_logging(settings.log_level)
    bot = WojBotV2(settings)
    bot.run(settings.token, log_handler=handler)


if __name__ == "__main__":
    main()
