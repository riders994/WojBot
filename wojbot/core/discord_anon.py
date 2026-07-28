"""Bot-local surrogate map for Discord IDs.

Keeps real Discord IDs off the shared warehouse. The elo package already stores
opaque bigints in ``dim_league.discord_server_id`` / ``dim_manager.discord_id``
(sync seeds them with a random bigint); the only place a *real* ID would leak in
is the bot writing ``guild_id`` / ``user.id`` directly. This maps each real ID to
an opaque **surrogate bigint** — the surrogate is what gets stored, the real
value is held only here — so the DB never sees a real Discord ID.

One JSON file, ``{category: {surrogate: real_id}}``, mirroring the elo package's
``{token: original}`` maps but numeric so it fits the bigint columns. It lives in
``resources/anon/`` (gitignored) beside those maps.

This is the interim approach; the plan is to later extend the package's
anonymizer to emit bigint tokens and fold these columns into it.
"""

from __future__ import annotations

import json
import os
import random
import threading
from pathlib import Path

# Category namespaces: server ids (dim_league) and manager ids (dim_manager) are
# different value spaces, so they get their own token maps.
CATEGORY_SERVER = "server"
CATEGORY_MANAGER = "manager"

# Surrogates are positive and fit a signed 64-bit (Postgres bigint) column;
# 2**62 leaves plenty of headroom below the 2**63-1 max.
_SURROGATE_BITS = 62


def _is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and value != value)  # NaN


class DiscordAnon:
    """Maps real Discord IDs to stored surrogate bigints, and back."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        # {category: {surrogate(str): real_id(int)}}
        self._by_surrogate: dict[str, dict[str, int]] = self._load()
        # {category: {real_id(int): surrogate(int)}}
        self._by_real: dict[str, dict[int, int]] = {
            cat: {real: int(sur) for sur, real in m.items()}
            for cat, m in self._by_surrogate.items()
        }

    def _load(self) -> dict[str, dict[str, int]]:
        if not self.path.exists():
            return {}
        with open(self.path) as f:
            data = json.load(f) or {}
        return {
            cat: {str(sur): int(real) for sur, real in m.items()}
            for cat, m in data.items()
        }

    def surrogate(self, category: str, real_id: int) -> int:
        """The surrogate for ``real_id``, minting and persisting one if new.

        Idempotent: the same real ID always returns the same surrogate.
        """
        real_id = int(real_id)
        with self._lock:
            reals = self._by_real.setdefault(category, {})
            if real_id in reals:
                return reals[real_id]
            surrogates = self._by_surrogate.setdefault(category, {})
            surrogate = self._mint(surrogates)
            surrogates[str(surrogate)] = real_id
            reals[real_id] = surrogate
            self._persist()
            return surrogate

    @staticmethod
    def _mint(existing: dict[str, int]) -> int:
        while True:
            surrogate = random.getrandbits(_SURROGATE_BITS)
            if surrogate and str(surrogate) not in existing:
                return surrogate

    def real(self, category: str, surrogate) -> int | None:
        """The real Discord ID behind a stored surrogate, or None if unknown.

        Unknown means the value never came from this bot — e.g. an opaque id
        sync seeded — so it stays hidden.
        """
        if _is_missing(surrogate):
            return None
        try:
            key = str(int(surrogate))
        except (TypeError, ValueError):
            # Non-numeric (e.g. an id_generator seed like "A3B9XZ12") is never
            # one of our surrogates.
            return None
        return self._by_surrogate.get(category, {}).get(key)

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._by_surrogate, f, indent=2)
        os.replace(tmp, self.path)
