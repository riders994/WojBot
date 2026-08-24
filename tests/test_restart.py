"""Tests for how a restart reports itself (:mod:`wojbot.core.restart`).

The marker is the only thing carrying a reason across a process boundary, so
most of this is about it surviving, being consumed exactly once, and failing
towards 'disruption' whenever it can't be trusted.
"""

from __future__ import annotations

import json
import random

import pytest

from wojbot.core.restart import (
    COMMANDED,
    DISRUPTION,
    FALLBACKS,
    load_messages,
    mark_commanded,
    pick_message,
    reason_from_marker,
    take_marker,
)


@pytest.fixture
def marker(tmp_path):
    return tmp_path / "state" / "restart.json"


# --- the marker -----------------------------------------------------------


def test_a_marked_shutdown_reads_back_as_commanded(marker):
    mark_commanded(4242, path=marker)
    assert reason_from_marker(take_marker(path=marker)) == COMMANDED


def test_the_marker_records_who_asked(marker):
    mark_commanded(4242, path=marker)
    assert take_marker(path=marker)["user_id"] == 4242


def test_mark_creates_the_state_directory(marker):
    assert not marker.parent.exists()
    mark_commanded(1, path=marker)
    assert marker.exists()


def test_no_marker_is_a_disruption(marker):
    assert take_marker(path=marker) is None
    assert reason_from_marker(None) == DISRUPTION


def test_reading_the_marker_consumes_it(marker):
    """Otherwise every restart after a commanded one inherits its reason."""
    mark_commanded(1, path=marker)
    assert take_marker(path=marker) is not None
    assert not marker.exists()
    assert take_marker(path=marker) is None


def test_a_corrupt_marker_still_counts_as_commanded(marker):
    """Something wrote it, and only the shutdown path ever does."""
    marker.parent.mkdir(parents=True)
    marker.write_text("{not json", encoding="utf-8")
    assert reason_from_marker(take_marker(path=marker)) == COMMANDED
    assert not marker.exists()


def test_an_unreadable_marker_is_still_removed(marker):
    """A marker that can't be parsed must not outlive its one restart."""
    marker.parent.mkdir(parents=True)
    marker.write_text('["not", "an", "object"]', encoding="utf-8")
    assert reason_from_marker(take_marker(path=marker)) == COMMANDED
    assert not marker.exists()


def test_a_marker_naming_another_reason_is_a_disruption():
    assert reason_from_marker({"reason": DISRUPTION}) == DISRUPTION


# --- the messages file ----------------------------------------------------


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_messages_load_per_reason(tmp_path):
    path = write(tmp_path / "m.json", {COMMANDED: ["a", "b"], DISRUPTION: ["c"]})
    assert load_messages(path=path) == {COMMANDED: ["a", "b"], DISRUPTION: ["c"]}


def test_blank_and_non_string_lines_are_dropped(tmp_path):
    path = write(tmp_path / "m.json", {COMMANDED: ["a", "", "  ", 7, None, "b"]})
    assert load_messages(path=path) == {COMMANDED: ["a", "b"]}


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_messages(path=tmp_path / "nope.json") == {}


def test_a_broken_file_is_not_an_error(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{oops", encoding="utf-8")
    assert load_messages(path=path) == {}


def test_a_reason_whose_value_is_not_a_list_is_ignored(tmp_path):
    path = write(tmp_path / "m.json", {COMMANDED: "a string", DISRUPTION: ["c"]})
    assert load_messages(path=path) == {DISRUPTION: ["c"]}


# --- picking one ----------------------------------------------------------


def test_a_line_comes_from_its_own_reason():
    messages = {COMMANDED: ["asked"], DISRUPTION: ["fell over"]}
    assert pick_message(COMMANDED, messages) == "asked"
    assert pick_message(DISRUPTION, messages) == "fell over"


def test_picking_is_random_across_the_list():
    messages = {COMMANDED: [str(n) for n in range(10)]}
    seen = {pick_message(COMMANDED, messages, rng=random.Random(seed)) for seed in range(50)}
    assert len(seen) > 1


def test_an_empty_list_falls_back_rather_than_saying_nothing():
    assert pick_message(COMMANDED, {COMMANDED: []}) == FALLBACKS[COMMANDED]
    assert pick_message(DISRUPTION, {}) == FALLBACKS[DISRUPTION]


# --- the shipped file -----------------------------------------------------


def test_the_shipped_messages_file_has_both_lists():
    messages = load_messages()
    assert messages[COMMANDED] and messages[DISRUPTION]
