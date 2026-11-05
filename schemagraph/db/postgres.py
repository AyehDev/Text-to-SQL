"""PostgreSQL access (SPEC 5.2).

Every connection is built through `config.postgres.conninfo(role, db_id)`:

  - `run_admin_query` uses the admin role. Extraction and inference only.
  - everything else uses the read-only role, which is also the only role that
    ever sees LLM-generated SQL.

A new connection is opened per call. That is fine at our scale; swap in
psycopg_pool later if latency matters.

None of these functions pass parameters to the generated SQL, so literal "%"
characters (e.g. LIKE '%abc%') are sent to PostgreSQL untouched.
"""

from __future__ import annotations

from typing import Any, Literal

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row

from schemagraph.config import DB_ID_RE, get_config
from schemagraph.models import ExplainResult, QueryResult
from schemagraph.sql_validation import is_single_select

Role = Literal["admin", "readonly"]

# Maintenance database, used only to list the other databases.
_SYSTEM_DB = "postgres"


# ---------------------------------------------------------------------------
# Helpers (pure ones are unit tested)
# ---------------------------------------------------------------------------


def _connect(role: Role, db_id: str, **kwargs: Any) -> psycopg.Connection:
    return psycopg.connect(get_config().postgres.conninfo(role, db_id), **kwargs)


def _strip_trailing_semicolon(sql: str) -> str:
    """Drop one trailing ';' so the SQL can be wrapped (EXPLAIN, DECLARE CURSOR).

    Safe because is_single_select() has already guaranteed a single statement.
    """
    return sql.rstrip().rstrip(";").rstrip()


def _to_str(value: object) -> str | None:
    """Serialize one cell: strings or null only (avoids date/decimal JSON issues)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    return str(value)


def _error_message(exc: psycopg.Error) -> str:
    """Database error text suited to feeding back into the LLM retry prompt."""
    diag = exc.diag
    message = diag.message_primary or str(exc).strip()
    if diag.message_hint:
        message += f" HINT: {diag.message_hint}"
    return message


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_databases() -> list[str]:
    """Databases the read-only role can connect to (excluding templates and 'postgres').

    This is the cluster's view. /api/databases will likely want to intersect it
    with the databases whose graph has actually been built.
    """
    query = """
        SELECT datname
        FROM pg_database
        WHERE NOT datistemplate
          AND datname <> 'postgres'
          AND has_database_privilege(current_user, datname, 'CONNECT')
        ORDER BY datname
    """
    with _connect("readonly", _SYSTEM_DB) as conn:
        rows = conn.execute(query).fetchall()
    # Names that fail the db_id rule could never be queried, so don't offer them.
    return [name for (name,) in rows if DB_ID_RE.fullmatch(name)]


def run_readonly_query(db_id: str, sql: str, row_limit: int) -> QueryResult:
    """Run LLM-generated (or gold) SQL as the read-only role.

    Only one read-only SELECT statement is accepted. Unsafe SQL raises
    ValueError before opening a database connection.

    Raises psycopg.Error on failure; the pipeline turns that into
    status="execution_error". Connection failures raise
    psycopg.OperationalError; the API turns that into HTTP 503.
    """
    if not is_single_select(sql):
        raise ValueError("Only a single read-only SELECT statement is allowed.")

    with _connect("readonly", db_id) as conn:
        conn.read_only = True
        # Server-side cursor: we pull at most row_limit + 1 rows instead of
        # loading a huge result set into memory. The extra row tells us
        # whether the result was truncated.
        with conn.cursor(name="sg_readonly_cursor") as cur:
            cur.execute(_strip_trailing_semicolon(sql))
            columns = [col.name for col in (cur.description or [])]
            fetched = cur.fetchmany(row_limit + 1)

    truncated = len(fetched) > row_limit
    rows = [[_to_str(v) for v in row] for row in fetched[:row_limit]]
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)


def explain(db_id: str, sql: str) -> ExplainResult:
    """Ask PostgreSQL to plan the query (EXPLAIN, never ANALYZE) without running it.

    Returns ok=False for unsafe or invalid SQL. Connection failures are NOT
    caught here; they raise.
    """
    if not is_single_select(sql):
        return ExplainResult(ok=False, error="Only a single read-only SELECT statement is allowed.")

    with _connect("readonly", db_id) as conn:
        conn.read_only = True
        try:
            conn.execute("EXPLAIN " + _strip_trailing_semicolon(sql))
        except psycopg.Error as exc:
            conn.rollback()
            return ExplainResult(ok=False, error=_error_message(exc))
    return ExplainResult(ok=True)


def run_admin_query(
    db_id: str,
    sql: str | pgsql.Composable,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extraction and inference only. NEVER pass LLM-generated SQL here.

    `params` uses psycopg's named style: "... WHERE x = %(x)s". When params is
    given, a literal "%" in the SQL must be written "%%". Identifiers cannot be
    parameters; build those queries with psycopg.sql.Identifier and pass the
    result as `sql` (hence the widened type versus the spec's `str`).
    """
    with _connect("admin", db_id, row_factory=dict_row) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []