"""SQL validation and read-only database validation."""

from __future__ import annotations

from schemagraph import db
from schemagraph.models import ExplainResult
from schemagraph.sql_validation import is_single_select

_UNSAFE_SQL_ERROR = "Only a single read-only SELECT statement is allowed."


def validate(db_id: str, sql: str) -> ExplainResult:
    """Validate one SELECT with PostgreSQL without executing the query."""
    if not is_single_select(sql):
        return ExplainResult(ok=False, error=_UNSAFE_SQL_ERROR)
    return db.explain(db_id, sql)
