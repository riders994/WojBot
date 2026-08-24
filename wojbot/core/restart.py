"""Why the bot last went down, and what it says about it when it comes back.

The bot cannot see its own restart: the process that knows why it is stopping
is not the process that starts. So ``/restart`` leaves a marker behind on its
way out, and the next start reads it -- a marker means the restart was asked
for, its absence means something else took the bot down. Reading it also
consumes it, so one shutdown produces exactly one announcement.

The lines themselves are data, not code: :data:`MESSAGES_PATH` holds one list
per reason, and editing that file is the whole of changing what the bot says.

The owner is told by DM whatever happens. A *server* is told only if it asked
to be: :func:`notify_targets` is the guest list, read from each server's own
settings. Both hear the same line, because one restart is one event and a bot
telling two rooms different stories about it reads as two restarts.
"""

from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT
from .defaults import DEFAULT_CHANNEL_KEY

log = logging.getLogger(__name__)

# Whether a server has asked to hear about restarts. The channel it hears in is
# its default messaging channel, not a restart-specific one -- see
# wojbot.core.defaults.DEFAULT_CHANNEL_KEY.
RESTART_NOTICE_KEY = "restart_notices"

# The two reasons, which are also the two keys in the messages file.
COMMANDED = "commanded"
DISRUPTION = "disruption"

MESSAGES_PATH = PROJECT_ROOT / "resources" / "restart_messages.json"
# Runtime state rather than a resource: written by the shutdown, read once by
# the start after it. Gitignored, and it has to be -- an update pulls over this
# directory between the two halves of one restart.
MARKER_PATH = PROJECT_ROOT / "resources" / "state" / "restart.json"

# Said when the file has nothing to say -- an empty list, a missing key, a file
# that isn't there yet. Silence would be indistinguishable from a bug.
FALLBACKS = {
    COMMANDED: "Back up, as ordered.",
    DISRUPTION: "Back up. I don't know what took me down.",
}


def mark_commanded(user_id: int | None = None, *, path: Path | None = None) -> None:
    """Record that the shutdown about to happen was asked for.

    Never raises: this runs in the last moment before the bot closes, and a
    restart that works but reports itself as a crash is a better outcome than
    one that fails at the door.
    """
    path = MARKER_PATH if path is None else path
    payload = {
        "reason": COMMANDED,
        "user_id": user_id,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        log.exception("Couldn't write the restart marker at %s", path)


def take_marker(*, path: Path | None = None) -> dict | None:
    """Read the marker and delete it, or return None if there isn't one.

    Deleting is the point: a marker left in place would make every restart
    after a commanded one look commanded too.
    """
    path = MARKER_PATH if path is None else path
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        log.exception("Couldn't read the restart marker at %s", path)
        return None
    finally:
        # Unlinked whether or not it parsed. A marker that can't be read is
        # still a marker that has done its job once.
        try:
            os.unlink(path)
        except OSError:
            pass
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Restart marker at %s wasn't valid JSON; treating it as bare", path)
        return {"reason": COMMANDED}
    return data if isinstance(data, dict) else {"reason": COMMANDED}


def reason_from_marker(marker: dict | None) -> str:
    """Which list to draw from. Anything but a marker saying otherwise is a disruption."""
    if marker is None:
        return DISRUPTION
    return COMMANDED if marker.get("reason", COMMANDED) == COMMANDED else DISRUPTION


def load_messages(*, path: Path | None = None) -> dict[str, list[str]]:
    """The per-reason lines from the messages file.

    Tolerant on purpose -- this is a file somebody hand-edits between restarts.
    A missing file, a missing key, or a non-string in a list costs the line,
    not the announcement.
    """
    path = MESSAGES_PATH if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("No restart messages file at %s", path)
        return {}
    except (OSError, json.JSONDecodeError):
        log.exception("Couldn't read restart messages from %s", path)
        return {}
    if not isinstance(data, dict):
        log.warning("Restart messages at %s aren't an object; ignoring", path)
        return {}
    return {
        str(reason): [line for line in lines if isinstance(line, str) and line.strip()]
        for reason, lines in data.items()
        if isinstance(lines, list)
    }


def pick_message(
    reason: str,
    messages: dict[str, list[str]] | None = None,
    *,
    rng: random.Random | None = None,
) -> str:
    """One line for ``reason``, at random, or the fallback if there are none."""
    messages = load_messages() if messages is None else messages
    lines = messages.get(reason) or []
    if not lines:
        return FALLBACKS.get(reason, FALLBACKS[DISRUPTION])
    return (rng or random).choice(lines)


def notify_targets(configs, guild_ids) -> list[tuple[int, int]]:
    """``(guild_id, channel_id)`` for every server that asked to be told.

    Two settings have to agree, and they are separate on purpose: subscribing
    says a server wants the notices, and the default channel says where the bot
    speaks to it at all. A server that turns notices on without a channel is
    asking for something the bot has nowhere to put, so it is skipped and said
    so in the log -- silently dropping it would look identical to the
    announcement failing.

    Only servers the bot is currently in are considered. A stored subscription
    for a server it has been removed from is not an error and not worth a line;
    it simply has nobody to tell.

    Pure, and reads nothing but config: deciding who to tell is a rule, and
    doing the telling is Discord's problem. See
    :meth:`wojbot.cogs.admin.Admin._tell_servers`.
    """
    targets = []
    for guild_id in guild_ids:
        config = configs.get_guild(guild_id)
        if not config.get(RESTART_NOTICE_KEY):
            continue
        channel_id = config.get(DEFAULT_CHANNEL_KEY)
        if channel_id is None:
            log.warning(
                "Server %s wants restart notices but has no default channel; "
                "nothing to announce to", guild_id,
            )
            continue
        targets.append((guild_id, channel_id))
    return targets
