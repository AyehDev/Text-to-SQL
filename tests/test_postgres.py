"""Unit tests for the pure helpers in db/postgres.py. No database needed."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from schemagraph.db.postgres import _strip_trailing_semicolon, _to_str


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        (1, "1"),
        (0, "0"),
        (3.5, "3.5"),
        (Decimal("8248.9900"), "8248.9900"),  # trailing zeros preserved
        ("Jon", "Jon"),
        ("", ""),  # empty string is NOT null
        (True, "true"),  # not "True": matches PostgreSQL's own text form
        (False, "false"),
        (date(2024, 1, 31), "2024-01-31"),
        (datetime(2024, 1, 31, 8, 30), "2024-01-31 08:30:00"),
        (b"\x00\xff", "\\x00ff"),
        (bytearray(b"\x01"), "\\x01"),
        (memoryview(b"\x0a"), "\\x0a"),
    ],
)
def test_to_str(value, expected):
    assert _to_str(value) == expected


def test_to_str_never_returns_non_string():
    for value in (1, 2.0, Decimal("1"), True, date.today(), b"x"):
        assert isinstance(_to_str(value), str)


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT 1", "SELECT 1"),
        ("SELECT 1;", "SELECT 1"),
        ("SELECT 1 ;  ", "SELECT 1"),
        ("  SELECT 1;\n", "  SELECT 1"),
        ("SELECT 1;;", "SELECT 1"),
        ("SELECT ';' AS semi", "SELECT ';' AS semi"),  # inner ';' untouched
        ("SELECT 'a;'", "SELECT 'a;'"),  # ';' inside a string at the end is kept
        ("", ""),
    ],
)
def test_strip_trailing_semicolon(sql, expected):
    assert _strip_trailing_semicolon(sql) == expected
