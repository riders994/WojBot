"""Shared helpers for rendering replies inside Discord's size limits.

Discord caps a message at 2000 characters and an embed description at 4096, and
a reply that goes over is rejected outright rather than trimmed. Anything the
bot builds from a variable number of rows has to decide in advance what to do
about that: :func:`clip` cuts the tail off, :func:`chunk` keeps all of it and
splits it across several messages.
"""

from __future__ import annotations

from collections.abc import Iterable

# Comfortably under Discord's 2000, leaving room for a header or a suffix.
MESSAGE_LIMIT = 1900


def clip(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Keep a message under Discord's limit, saying so when it had to cut."""
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


def chunk(blocks: Iterable[str], limit: int = MESSAGE_LIMIT, sep: str = "\n") -> list[str]:
    """Group ``blocks`` into as few messages as fit, without splitting one.

    For listings where dropping the tail would be the wrong answer -- a whole
    season of rumors is meant to be a whole season. A block longer than
    ``limit`` on its own is clipped, since nothing else can be done with it.
    """
    messages: list[str] = []
    current: list[str] = []
    length = 0
    for block in blocks:
        block = clip(block, limit)
        # +len(sep) for the separator this block would need if it joined the
        # current message; the first block in a message doesn't pay it.
        cost = len(block) + (len(sep) if current else 0)
        if current and length + cost > limit:
            messages.append(sep.join(current))
            current, length = [block], len(block)
        else:
            current.append(block)
            length += cost
    if current:
        messages.append(sep.join(current))
    return messages
