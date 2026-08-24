"""Tests for the restart command's supervision check.

The rest of the cog is Discord plumbing; this is the part with a rule in it --
exiting is only a restart if something is watching for the exit.
"""

from __future__ import annotations

from wojbot.cogs.admin import supervised


def test_systemd_sets_invocation_id():
    """The variable systemd puts in every service's environment."""
    assert supervised({"INVOCATION_ID": "8b1a9953c4611296a827abf8c47804d7"})


def test_a_bare_shell_is_not_supervised():
    assert not supervised({"PATH": "/usr/bin", "HOME": "/home/piders994"})


def test_an_empty_invocation_id_still_counts():
    """Presence is the signal; systemd never writes an empty one."""
    assert supervised({"INVOCATION_ID": ""})


def test_the_real_environment_is_the_default(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "x")
    assert supervised()
    monkeypatch.delenv("INVOCATION_ID")
    assert not supervised()
