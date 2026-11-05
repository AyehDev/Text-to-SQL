"""SQL validation and read-only database validation."""

from __future__ import annotations

from schemagraph import db
from schemagraph.models import ExplainResult
from schemagraph.sql_validation import check_select


def validate(db_id: str, sql: str) -> ExplainResult:
    """Validate one SELECT with PostgreSQL without executing the query.

    Unsafe SQL is rejected before any connection is opened. The error says what was
    wrong so the retry loop can pass it back to the model.
    """
    check = check_select(sql)
    if not check.ok:
        return ExplainResult(ok=False, error=check.reason)
    return db.explain(db_id, sql)
