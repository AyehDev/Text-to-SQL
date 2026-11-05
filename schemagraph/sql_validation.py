"""Validation helpers for SQL accepted from the language model."""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


def is_single_select(statement: str) -> bool:
    """Return whether SQL is exactly one read-only SELECT statement.

    Parsing uses PostgreSQL syntax. Data-modifying CTEs, SELECT INTO, and
    locking SELECTs are rejected as well as non-SELECT or multiple statements.
    """
    if not isinstance(statement, str) or not statement.strip():
        return False

    try:
        statements = [node for node in sqlglot.parse(statement, read="postgres") if node]
    except ParseError:
        return False

    if len(statements) != 1:
        return False

    query = statements[0]
    if not isinstance(query, (exp.Select, exp.SetOperation)):
        return False

    if any(
        isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Merge))
        for node in query.walk()
    ):
        return False

    for select in query.find_all(exp.Select):
        if select.args.get("into") or select.args.get("locks"):
            return False

    return True
