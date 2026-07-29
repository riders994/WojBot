"""Tests for settings resolution in :mod:`wojbot.core.config`.

:func:`resolve_sql_uri` takes the environment as a mapping rather than reading
``os.environ``, so these pass one in and never touch the real environment or the
developer's own ``.env``.
"""

from __future__ import annotations

import pytest

from wojbot.core.config import DEFAULT_SQL_PORT, resolve_sql_uri
from wojbot.core.errors import ConfigError

FIELDS = {
    "SQL_HOST": "localhost",
    "SQL_PORT": "5432",
    "SQL_USER": "pylot",
    "SQL_PASSWORD": "hunter2",
    "SQL_DBNAME": "wojbot_db",
}


# --- which source wins ----------------------------------------------------


def test_uri_is_used_when_set():
    uri = "postgresql://someone@example:5432/db?sslmode=require"
    assert resolve_sql_uri({"SQL_CONN_URI": uri}) == uri


def test_uri_wins_over_the_fields():
    """A pasted URI is the more specific instruction, so the fields stand down."""
    uri = "postgresql://someone@example:5432/db"
    assert resolve_sql_uri({"SQL_CONN_URI": uri, **FIELDS}) == uri


def test_nothing_configured_means_no_database():
    assert resolve_sql_uri({}) is None
    assert resolve_sql_uri({"SQL_CONN_URI": "", "SQL_HOST": ""}) is None


def test_blank_uri_falls_through_to_the_fields():
    """An empty SQL_CONN_URI= line is 'unset', not 'connect to nothing'."""
    assert resolve_sql_uri({"SQL_CONN_URI": "   ", **FIELDS}) == (
        "postgresql://pylot:hunter2@localhost:5432/wojbot_db"
    )


# --- assembling from the fields -------------------------------------------


def test_fields_are_assembled():
    assert resolve_sql_uri(FIELDS) == "postgresql://pylot:hunter2@localhost:5432/wojbot_db"


def test_port_defaults():
    env = {**FIELDS, "SQL_PORT": ""}
    assert resolve_sql_uri(env).endswith(f":{DEFAULT_SQL_PORT}/wojbot_db")


def test_empty_password_omits_the_colon():
    """Peer and trust auth have no password; a trailing ':' would be wrong."""
    env = {**FIELDS, "SQL_PASSWORD": ""}
    assert resolve_sql_uri(env) == "postgresql://pylot@localhost:5432/wojbot_db"


def test_values_are_stripped():
    env = {key: f"  {value}  " for key, value in FIELDS.items()}
    assert resolve_sql_uri(env) == "postgresql://pylot:hunter2@localhost:5432/wojbot_db"


# --- the escaping that makes the fields worth having ----------------------


def test_password_special_characters_are_encoded():
    """The whole point: an @ or / in a password must not split the URI."""
    env = {**FIELDS, "SQL_PASSWORD": "p@ss/w:rd?#"}
    uri = resolve_sql_uri(env)
    assert uri == "postgresql://pylot:p%40ss%2Fw%3Ard%3F%23@localhost:5432/wojbot_db"
    # Exactly one @ separating credentials from host, wherever it came from.
    assert uri.count("@") == 1


def test_username_special_characters_are_encoded():
    env = {**FIELDS, "SQL_USER": "some one@corp"}
    assert resolve_sql_uri(env).startswith("postgresql://some%20one%40corp:")


def test_ipv6_host_is_bracketed():
    """Unbracketed, the port would read as another group of the address."""
    env = {**FIELDS, "SQL_HOST": "::1"}
    assert resolve_sql_uri(env) == "postgresql://pylot:hunter2@[::1]:5432/wojbot_db"


def test_already_bracketed_ipv6_is_left_alone():
    env = {**FIELDS, "SQL_HOST": "[fe80::1]"}
    assert "[[" not in resolve_sql_uri(env)


def test_hostname_is_not_bracketed():
    assert "[" not in resolve_sql_uri(FIELDS)


# --- half-configured is an error, not a silent fallback -------------------


@pytest.mark.parametrize("missing", ["SQL_HOST", "SQL_USER", "SQL_DBNAME"])
def test_a_missing_required_field_raises(missing):
    env = {**FIELDS, missing: ""}
    with pytest.raises(ConfigError) as excinfo:
        resolve_sql_uri(env)
    assert missing in str(excinfo.value)


def test_the_error_names_every_missing_field():
    env = {"SQL_HOST": "localhost"}
    with pytest.raises(ConfigError) as excinfo:
        resolve_sql_uri(env)
    message = str(excinfo.value)
    assert "SQL_USER" in message and "SQL_DBNAME" in message


def test_only_a_password_still_raises():
    """Otherwise a typo'd host name would quietly disable the database."""
    with pytest.raises(ConfigError):
        resolve_sql_uri({"SQL_PASSWORD": "hunter2"})
