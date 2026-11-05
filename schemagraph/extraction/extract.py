"""Metadata extraction: PostgreSQL catalog -> DatabaseMetadata (SPEC 5.3).

Run the five queries in extraction/sql/, assemble the shared models, and save
the result as data/metadata/{db_id}.json for graph ingestion.

Structure: the `build_*` functions are pure (row dicts in, models out) so they
can be unit tested without a database. The `fetch_*` functions run the SQL and
call them. Only `run_admin_query` ever touches the database; this module never
sees the read-only role or any LLM-generated SQL.

Names are used exactly as the catalog stores them. UIDs are built with the
helpers in models.py and never parsed back apart, because a "." inside a table
or column name would make that ambiguous; every ColumnMeta carries its
`table_uid` explicitly instead.
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

import psycopg
from psycopg import sql as pgsql

from schemagraph import db
from schemagraph.models import (
    ColumnMeta,
    DatabaseMetadata,
    RelationshipMeta,
    SchemaMeta,
    TableMeta,
    column_uid as make_column_uid,
    table_uid,
)

log = logging.getLogger(__name__)

SQL_DIR = Path(__file__).parent / "sql"
METADATA_DIR = Path("data/metadata")

# Sample values are only a hint for describing columns and checking value overlap.
SAMPLE_SCAN_ROWS = 1000  # look at this many rows, then keep the first n distinct
SAMPLE_MAX_CHARS = 100  # truncate long text so JSON and prompts stay small
SKIP_SAMPLE_TYPES = frozenset({"bytea", "xml", "tsvector", "tsquery"})


class ExtractionError(RuntimeError):
    """The extracted metadata is inconsistent (see the message for details)."""


def default_metadata_path(db_id: str) -> Path:
    return METADATA_DIR / f"{db_id}.json"


@lru_cache(maxsize=None)
def _read_sql(name: str) -> str:
    return (SQL_DIR / name).read_text()


def _rows(db_id: str, name: str) -> list[dict[str, Any]]:
    return db.run_admin_query(db_id, _read_sql(name))


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------


def group_primary_keys(db_id: str, rows: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """table_uid -> primary-key column names, in key order."""
    keyed: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for r in rows:
        uid = table_uid(db_id, r["schema_name"], r["table_name"])
        keyed[uid].append((r["key_position"], r["column_name"]))
    return {uid: [col for _, col in sorted(cols)] for uid, cols in keyed.items()}


def group_unique_sets(db_id: str, rows: Iterable[dict[str, Any]]) -> dict[str, list[frozenset[str]]]:
    """table_uid -> one set of column names per unique index."""
    by_index: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for r in rows:
        by_index[(r["schema_name"], r["table_name"], r["index_name"])].add(r["column_name"])
    result: dict[str, list[frozenset[str]]] = defaultdict(list)
    for (schema, table, _), cols in sorted(by_index.items()):
        result[table_uid(db_id, schema, table)].append(frozenset(cols))
    return dict(result)


def single_unique_columns(db_id: str, rows: Iterable[dict[str, Any]]) -> set[str]:
    """Column UIDs that are unique on their own (a one-column unique index)."""
    by_index: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for r in rows:
        by_index[(r["schema_name"], r["table_name"], r["index_name"])].add(r["column_name"])
    return {
        make_column_uid(db_id, schema, table, next(iter(cols)))
        for (schema, table, _), cols in by_index.items()
        if len(cols) == 1
    }


def is_one_to_one(fk_columns: Iterable[str], unique_sets: Iterable[frozenset[str]]) -> bool:
    """A foreign key is 1:1 when its columns contain a whole unique column set.

    If some unique set U is inside the FK's columns, no two rows can share the
    same FK values, so each referenced row is matched at most once. This also
    covers composite keys (an extension table whose composite PK is the FK).
    """
    cols = set(fk_columns)
    return any(u <= cols for u in unique_sets)


def first_distinct(values: Iterable[str | None], n: int) -> list[str]:
    """First n distinct non-empty values, in the order first seen."""
    seen: dict[str, None] = {}
    for v in values:
        if v is None or not v.strip():
            continue
        seen.setdefault(v, None)
        if len(seen) >= n:
            break
    return list(seen)


def skip_sample_type(data_type: str) -> bool:
    """Binary and document types are useless (and huge) as sample values."""
    base = data_type.split("(")[0].replace("[]", "").strip().lower()
    return base in SKIP_SAMPLE_TYPES


# ---------------------------------------------------------------------------
# Pure builders: row dicts -> models (unit tested)
# ---------------------------------------------------------------------------


def build_tables(
    db_id: str,
    rows: Iterable[dict[str, Any]],
    primary_keys: dict[str, list[str]],
    count_fn: Callable[[str, str], int],
) -> list[TableMeta]:
    """row_count: the planner's estimate when usable, else an exact COUNT(*).

    The estimate is NULL for a never-analyzed table and 0 for an empty (or
    stale) one; in both cases an exact count is the safe answer.
    """
    tables = []
    for r in rows:
        schema, name = r["schema_name"], r["table_name"]
        uid = table_uid(db_id, schema, name)
        estimate = r["row_estimate"]
        row_count = estimate if estimate else count_fn(schema, name)
        description = r["description"] or None
        tables.append(
            TableMeta(
                uid=uid,
                schema=schema,
                name=name,
                description=description,
                description_source="catalog" if description else None,
                row_count=row_count,
                primary_key=primary_keys.get(uid, []),
            )
        )
    return tables


def build_columns(
    db_id: str,
    rows: Iterable[dict[str, Any]],
    primary_keys: dict[str, list[str]],
    unique_columns: set[str],
) -> list[ColumnMeta]:
    """Columns without sample values (those are added separately)."""
    columns = []
    for r in rows:
        schema, table, name = r["schema_name"], r["table_name"], r["column_name"]
        t_uid = table_uid(db_id, schema, table)
        uid = make_column_uid(db_id, schema, table, name)
        description = r["description"] or None
        columns.append(
            ColumnMeta(
                uid=uid,
                table_uid=t_uid,
                name=name,
                data_type=r["data_type"],
                ordinal_position=r["ordinal_position"],
                is_nullable=r["is_nullable"],
                is_primary_key=name in primary_keys.get(t_uid, []),
                is_unique=uid in unique_columns,
                description=description,
                description_source="catalog" if description else None,
            )
        )
    return columns


def build_relationships(
    db_id: str,
    fk_rows: Iterable[dict[str, Any]],
    unique_sets: dict[str, list[frozenset[str]]],
) -> list[RelationshipMeta]:
    """One RelationshipMeta per FK column pair; a composite FK gives several.

    All pairs of one constraint share a cardinality, decided on the whole set
    of the constraint's referencing columns. RelationshipMeta has no position
    field, so the result is simply sorted for stable output.
    """
    constraints: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in fk_rows:
        constraints[(r["from_schema"], r["from_table"], r["constraint_name"])].append(r)

    relationships = []
    for (schema, table, constraint), pairs in constraints.items():
        from_table_uid = table_uid(db_id, schema, table)
        one_to_one = is_one_to_one(
            (p["from_column"] for p in pairs), unique_sets.get(from_table_uid, [])
        )
        for p in pairs:
            relationships.append(
                RelationshipMeta(
                    from_column=make_column_uid(db_id, schema, table, p["from_column"]),
                    to_column=make_column_uid(db_id, p["to_schema"], p["to_table"], p["to_column"]),
                    source="declared",
                    confidence=1.0,
                    cardinality="1:1" if one_to_one else "N:1",
                    constraint_name=constraint,
                    evidence=None,
                )
            )
    relationships.sort(key=lambda r: (r.from_column, r.to_column, r.constraint_name or ""))
    return relationships


# ---------------------------------------------------------------------------
# Database-touching fetchers (SPEC 5.3)
# ---------------------------------------------------------------------------


def fetch_primary_keys(db_id: str) -> dict[str, list[str]]:
    """table_uid -> primary-key column names in key order."""
    return group_primary_keys(db_id, _rows(db_id, "primary_keys.sql"))


def fetch_unique_columns(db_id: str) -> set[str]:
    """Column UIDs that are unique on their own."""
    return single_unique_columns(db_id, _rows(db_id, "unique_constraints.sql"))


def _exact_count(db_id: str, schema: str, table: str) -> int:
    query = pgsql.SQL("SELECT count(*) AS n FROM {}").format(pgsql.Identifier(schema, table))
    return int(db.run_admin_query(db_id, query)[0]["n"])


def fetch_tables(
    db_id: str, primary_keys: dict[str, list[str]] | None = None
) -> list[TableMeta]:
    """Base tables. Pass `primary_keys` if you already fetched them."""
    if primary_keys is None:
        primary_keys = fetch_primary_keys(db_id)
    return build_tables(
        db_id,
        _rows(db_id, "tables.sql"),
        primary_keys,
        lambda schema, table: _exact_count(db_id, schema, table),
    )


def fetch_columns(
    db_id: str,
    primary_keys: dict[str, list[str]] | None = None,
    unique_columns: set[str] | None = None,
) -> list[ColumnMeta]:
    """Columns with empty sample_values; use fetch_sample_values to fill those."""
    if primary_keys is None:
        primary_keys = fetch_primary_keys(db_id)
    if unique_columns is None:
        unique_columns = fetch_unique_columns(db_id)
    return build_columns(db_id, _rows(db_id, "columns.sql"), primary_keys, unique_columns)


def fetch_foreign_keys(db_id: str) -> list[RelationshipMeta]:
    """Declared foreign keys (source="declared")."""
    unique_sets = group_unique_sets(db_id, _rows(db_id, "unique_constraints.sql"))
    return build_relationships(db_id, _rows(db_id, "foreign_keys.sql"), unique_sets)


def _sample_values(
    db_id: str, schema: str, table: str, column: str, data_type: str, n: int
) -> list[str]:
    if n <= 0 or skip_sample_type(data_type):
        return []
    # Read a bounded prefix and de-duplicate in Python. SELECT DISTINCT ... LIMIT
    # would have to scan the whole table before returning anything.
    query = pgsql.SQL(
        "SELECT left({col}::text, {max_chars}) AS v FROM {tbl} WHERE {col} IS NOT NULL LIMIT {scan}"
    ).format(
        col=pgsql.Identifier(column),
        tbl=pgsql.Identifier(schema, table),
        max_chars=pgsql.Literal(SAMPLE_MAX_CHARS),
        scan=pgsql.Literal(SAMPLE_SCAN_ROWS),
    )
    try:
        rows = db.run_admin_query(db_id, query)
    except psycopg.Error as exc:
        # One unreadable column must not abort the whole extraction.
        log.warning("no sample values for %s.%s.%s: %s", schema, table, column, exc)
        return []
    return first_distinct((r["v"] for r in rows), n)


class ColumnRef(NamedTuple):
    """A column's real catalog names, ready to be quoted into SQL."""

    schema: str
    table: str
    column: str
    data_type: str


def resolve_column(db_id: str, column_uid: str) -> ColumnRef:
    """Look a column UID up in the catalog.

    The UID is matched against the catalog instead of being split apart, so
    names containing "." still resolve correctly.
    """
    found = db.run_admin_query(
        db_id,
        """
        SELECT n.nspname AS schema_name, c.relname AS table_name,
               a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS data_type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE concat(%(db)s::text, ':', n.nspname, '.', c.relname, '.', a.attname) = %(uid)s
          AND a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p')
        """,
        {"db": db_id, "uid": column_uid},
    )
    if not found:
        raise ValueError(f"No such column in database {db_id!r}: {column_uid}")
    r = found[0]
    return ColumnRef(r["schema_name"], r["table_name"], r["column_name"], r["data_type"])


def fetch_sample_values(db_id: str, column_uid: str, n: int = 5) -> list[str]:
    """Up to n distinct non-null values of one column, as (truncated) strings."""
    ref = resolve_column(db_id, column_uid)
    return _sample_values(db_id, ref.schema, ref.table, ref.column, ref.data_type, n)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def extract_metadata(db_id: str, *, sample_n: int = 5) -> DatabaseMetadata:
    """Declared-only metadata for one database. `sample_n=0` skips sampling.

    Inferred relationships are added later by extraction/infer.py.
    """
    table_rows = _rows(db_id, "tables.sql")
    column_rows = _rows(db_id, "columns.sql")
    pk_rows = _rows(db_id, "primary_keys.sql")
    fk_rows = _rows(db_id, "foreign_keys.sql")
    unique_rows = _rows(db_id, "unique_constraints.sql")

    primary_keys = group_primary_keys(db_id, pk_rows)
    unique_sets = group_unique_sets(db_id, unique_rows)
    unique_columns = single_unique_columns(db_id, unique_rows)

    tables = build_tables(
        db_id,
        table_rows,
        primary_keys,
        lambda schema, table: _exact_count(db_id, schema, table),
    )
    columns = build_columns(db_id, column_rows, primary_keys, unique_columns)
    relationships = build_relationships(db_id, fk_rows, unique_sets)

    if sample_n > 0:
        # column_rows and columns are in the same order; use the raw rows for
        # the schema/table/column names so no UID is ever split apart.
        columns = [
            col.model_copy(
                update={
                    "sample_values": _sample_values(
                        db_id, r["schema_name"], r["table_name"], r["column_name"], col.data_type, sample_n
                    )
                }
            )
            for col, r in zip(columns, column_rows, strict=True)
        ]

    schemas = [SchemaMeta(name=s) for s in sorted({t.schema_ for t in tables})]
    meta = DatabaseMetadata(
        db_id=db_id,
        extracted_at=datetime.now(timezone.utc),
        schemas=schemas,
        tables=tables,
        columns=columns,
        relationships=relationships,
    )

    problems = meta.check_integrity()
    if problems:
        shown = "\n  ".join(problems[:20])
        raise ExtractionError(f"{len(problems)} integrity problem(s) in {db_id}:\n  {shown}")

    odd = [t.uid for t in tables if "." in t.name or ":" in t.name] + [
        c.uid for c in columns if "." in c.name or ":" in c.name
    ]
    if odd:
        log.warning(
            "%d name(s) contain '.' or ':'; their UIDs are ambiguous to anything that "
            "splits them (first: %s)",
            len(odd),
            odd[0],
        )
    return meta


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------


def save_metadata(meta: DatabaseMetadata, path: Path) -> None:
    """Write JSON atomically, using the contract's field names ("schema")."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = meta.model_dump_json(by_alias=True, indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_metadata(path: Path) -> DatabaseMetadata:
    return DatabaseMetadata.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Command line:  python -m schemagraph.extraction.extract --db adventureworks
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract database metadata to JSON.")
    parser.add_argument("--db", required=True, help="db_id (the PostgreSQL database name)")
    parser.add_argument("--out", type=Path, help="output file (default: data/metadata/<db>.json)")
    parser.add_argument("--sample-n", type=int, default=5, help="sample values per column; 0 = none")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    meta = extract_metadata(args.db, sample_n=args.sample_n)
    out = args.out or default_metadata_path(args.db)
    save_metadata(meta, out)

    one_to_one = sum(r.cardinality == "1:1" for r in meta.relationships)
    print(
        f"{meta.db_id}: {len(meta.schemas)} schemas, {len(meta.tables)} tables, "
        f"{len(meta.columns)} columns, {len(meta.relationships)} relationships "
        f"({one_to_one} one-to-one) -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())