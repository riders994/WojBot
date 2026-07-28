"""Logging setup for WojBot.

Replaces v1's no-op setup (a handler with no level that logged empty strings).
Emits to both a rotating file and stdout so that ``journalctl`` picks it up when
running under systemd on the Raspberry Pi.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_LOG_FILE = Path("wojbot.log")


def configure_logging(level: str = "INFO") -> logging.Handler:
    """Configure root logging and return a handler for ``bot.run(log_handler=...)``.

    Attaches a stdout stream handler and a rotating file handler to the root
    logger, then returns the file handler so discord.py logs through the same
    file. Returning a handler (rather than letting discord.py install its own)
    keeps all logging in one place.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter(_FORMAT)

    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    # Avoid piling up duplicate handlers if called more than once.
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    return file_handler
