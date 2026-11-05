"""Stable public API for PostgreSQL access.

Callers should use these operations rather than depending on the implementation
module or opening psycopg connections themselves.
"""

from __future__ import annotations

from typing import Any

from psycopg import sql as pgsql

from schemagraph.models import ExplainResult, QueryResult

from . import postgres as _postgres

__all__ = [
    "list_databases",
    "run_readonly_query",
    "explain",
    "run_admin_query",
]


def list_databases() -> list[str]:
    """List databases accessible to the configured read-only role."""
    return _postgres.list_databases()


def run_readonly_query(db_id: str, sql: str, row_limit: int) -> QueryResult:
    """Execute one read-only SELECT and return at most ``row_limit`` rows."""
    return _postgres.run_readonly_query(db_id, sql, row_limit)


def explain(db_id: str, sql: str) -> ExplainResult:
    """Validate and ask PostgreSQL to plan one read-only SELECT."""
    return _postgres.explain(db_id, sql)


def run_admin_query(
    db_id: str,
    sql: str | pgsql.Composable,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run trusted extraction or inference SQL using the admin role."""
    return _postgres.run_admin_query(db_id, sql, params)