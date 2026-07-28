"""Cog package with auto-discovery.

Drop a new ``*.py`` module in here that defines an ``async def setup(bot)`` and it
will be loaded automatically at startup. Modules whose names start with ``_`` are
skipped (helpers, work-in-progress). This is the single source of truth for which
extensions load — v1 had two disagreeing lists.
"""

from __future__ import annotations

from pkgutil import iter_modules


def discover_extensions() -> list[str]:
    """Return the fully-qualified module names of loadable cogs in this package."""
    return [
        f"{__package__}.{module.name}"
        for module in iter_modules(__path__)
        if not module.name.startswith("_")
    ]
