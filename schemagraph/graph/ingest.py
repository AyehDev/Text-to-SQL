"""Load DatabaseMetadata into Neo4j as the schema graph (SPEC 2, 5.6).

    (:Schema)-[:CONTAINS]->(:Table)-[:HAS_COLUMN]->(:Column)
    (:Column)-[:REFERENCES]->(:Column)     FK column -> referenced column

Everything that decides WHAT is written is pure (`build_rows`) and tested with
no database. The functions that talk to Neo4j are thin: each runs a fixed
Cypher statement over batches of rows. Every write is a MERGE, so ingesting
the same metadata twice gives the same graph; `clear_database` is how a table
that disappeared from the source gets removed.

Every function that touches Neo4j takes an optional `session` (a neo4j
Session, or a fake in tests) and otherwise opens one from graph.driver.

    python -m schemagraph.graph.ingest --db adventureworks --rebuild
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from schemagraph.extraction import extract
from schemagraph.graph.weights import edge_weights
from schemagraph.models import DatabaseMetadata, IngestReport, schema_uid

log = logging.getLogger(__name__)

BATCH_SIZE = 1000

# ---------------------------------------------------------------------------
# Cypher. Labels and relationship types cannot be parameters, so each node
# label has its own statement; row contents always travel as parameters.
# ---------------------------------------------------------------------------

CONSTRAINTS = [
    "CREATE CONSTRAINT schema_uid IF NOT EXISTS FOR (n:Schema) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT table_uid IF NOT EXISTS FOR (n:Table) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT column_uid IF NOT EXISTS FOR (n:Column) REQUIRE n.uid IS UNIQUE",
    "CREATE INDEX table_name IF NOT EXISTS FOR (n:Table) ON (n.name)",
    "CREATE INDEX column_name IF NOT EXISTS FOR (n:Column) ON (n.name)",
    "CREATE INDEX schema_db IF NOT EXISTS FOR (n:Schema) ON (n.db_id)",
    "CREATE INDEX table_db IF NOT EXISTS FOR (n:Table) ON (n.db_id)",
    "CREATE INDEX column_db IF NOT EXISTS FOR (n:Column) ON (n.db_id)",
]

MERGE_NODES = {
    label: f"UNWIND $rows AS row MERGE (n:{label} {{uid: row.uid}}) SET n += row"
    for label in ("Schema", "Table", "Column")
}

MERGE_CONTAINS = (
    "UNWIND $rows AS row "
    "MATCH (s:Schema {uid: row.src}) MATCH (t:Table {uid: row.dst}) "
    "MERGE (s)-[:CONTAINS]->(t)"
)
MERGE_HAS_COLUMN = (
    "UNWIND $rows AS row "
    "MATCH (t:Table {uid: row.src}) MATCH (c:Column {uid: row.dst}) "
    "MERGE (t)-[:HAS_COLUMN]->(c)"
)
MERGE_REFERENCES = (
    "UNWIND $rows AS row "
    "MATCH (a:Column {uid: row.src}) MATCH (b:Column {uid: row.dst}) "
    "MERGE (a)-[r:REFERENCES]->(b) SET r += row.props"
)

READ_UIDS = {label: f"MATCH (n:{label} {{db_id: $db_id}}) RETURN n.uid AS uid" for label in ("Schema", "Table", "Column")}
READ_CONTAINS = "MATCH (s:Schema {db_id: $db_id})-[:CONTAINS]->(t:Table) RETURN s.uid AS src, t.uid AS dst"
READ_HAS_COLUMN = "MATCH (t:Table {db_id: $db_id})-[:HAS_COLUMN]->(c:Column) RETURN t.uid AS src, c.uid AS dst"
READ_REFERENCES = (
    "MATCH (a:Column {db_id: $db_id})-[r:REFERENCES]->(b:Column) "
    "RETURN a.uid AS src, b.uid AS dst, r.source AS source, r.confidence AS confidence, "
    "r.cardinality AS cardinality, r.weight_forward AS weight_forward, r.weight_reverse AS weight_reverse"
)
SET_WEIGHTS = (
    "UNWIND $rows AS row "
    "MATCH (:Column {uid: row.src})-[r:REFERENCES]->(:Column {uid: row.dst}) "
    "SET r.weight_forward = row.weight_forward, r.weight_reverse = row.weight_reverse"
)
DELETE_BATCH = (
    "MATCH (n {db_id: $db_id}) WITH n LIMIT $limit DETACH DELETE n RETURN count(*) AS deleted"
)

# ---------------------------------------------------------------------------
# Pure part: metadata -> rows
# ---------------------------------------------------------------------------


@dataclass
class GraphRows:
    """Everything one database contributes to the graph."""

    schemas: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    columns: list[dict[str, Any]] = field(default_factory=list)
    contains: list[dict[str, str]] = field(default_factory=list)      # {src: schema uid, dst: table uid}
    has_column: list[dict[str, str]] = field(default_factory=list)    # {src: table uid, dst: column uid}
    references: list[dict[str, Any]] = field(default_factory=list)    # {src, dst, props}


def build_rows(meta: DatabaseMetadata) -> GraphRows:
    """Turn metadata into node and edge rows. Raises ValueError if it is inconsistent."""
    problems = meta.check_integrity()
    if problems:
        raise ValueError(f"metadata for {meta.db_id!r} is inconsistent: {problems[:5]}")

    db_id = meta.db_id
    rows = GraphRows()
    for s in meta.schemas:
        rows.schemas.append({"uid": schema_uid(db_id, s.name), "db_id": db_id, "name": s.name})

    for t in meta.tables:
        rows.tables.append({
            "uid": t.uid, "db_id": db_id, "schema": t.schema_, "name": t.name,
            "description": t.description, "description_source": t.description_source,
            "row_count": t.row_count, "primary_key": list(t.primary_key),
        })
        rows.contains.append({"src": schema_uid(db_id, t.schema_), "dst": t.uid})

    table_of: dict[str, str] = {}
    for c in meta.columns:
        table_of[c.uid] = c.table_uid
        rows.columns.append({
            "uid": c.uid, "db_id": db_id, "table_uid": c.table_uid, "name": c.name,
            "data_type": c.data_type, "ordinal_position": c.ordinal_position,
            "is_nullable": c.is_nullable, "is_primary_key": c.is_primary_key,
            "is_unique": c.is_unique, "description": c.description,
            "description_source": c.description_source, "sample_values": list(c.sample_values),
        })
        rows.has_column.append({"src": c.table_uid, "dst": c.uid})

    for r in meta.relationships:
        forward, reverse = edge_weights(r.confidence, r.cardinality)
        rows.references.append({
            "src": r.from_column,
            "dst": r.to_column,
            "props": {
                "db_id": db_id,
                "source": r.source,
                "confidence": r.confidence,
                "cardinality": r.cardinality,
                "constraint_name": r.constraint_name,
                # Neo4j properties cannot be maps, so the evidence is kept as JSON text.
                "evidence_json": json.dumps(r.evidence.model_dump(by_alias=True)) if r.evidence else None,
                "weight_forward": forward,
                "weight_reverse": reverse,
                # The planner searches over tables; keeping both ends here lets it
                # project the column graph onto tables without extra lookups.
                "from_table": table_of[r.from_column],
                "to_table": table_of[r.to_column],
            },
        })
    return rows


def compare(rows: GraphRows, actual: dict[str, Any]) -> list[str]:
    """Differences between the expected rows and what Neo4j holds (empty list = matches).

    `actual` has: schemas/tables/columns = sets of uids; contains/has_column =
    sets of (from, to); references = {(from, to): row dict}.
    """
    problems: list[str] = []

    def diff(kind: str, expected: set, found: set) -> None:
        for label, items in (("missing from graph", expected - found), ("unexpected in graph", found - expected)):
            if items:
                sample = sorted(map(str, items))[:3]
                problems.append(f"{len(items)} {kind} {label}, e.g. {sample}")

    diff("schemas", {r["uid"] for r in rows.schemas}, actual["schemas"])
    diff("tables", {r["uid"] for r in rows.tables}, actual["tables"])
    diff("columns", {r["uid"] for r in rows.columns}, actual["columns"])
    diff("CONTAINS edges", {(r["src"], r["dst"]) for r in rows.contains}, actual["contains"])
    diff("HAS_COLUMN edges", {(r["src"], r["dst"]) for r in rows.has_column}, actual["has_column"])

    expected_refs = {(r["src"], r["dst"]): r["props"] for r in rows.references}
    diff("REFERENCES edges", set(expected_refs), set(actual["references"]))
    for pair, props in expected_refs.items():
        found = actual["references"].get(pair)
        if found is None:
            continue
        for key in ("source", "confidence", "cardinality", "weight_forward", "weight_reverse"):
            if found.get(key) != props[key]:
                problems.append(f"REFERENCES {pair[0]} -> {pair[1]}: {key} is {found.get(key)!r}, expected {props[key]!r}")
    return problems


# ---------------------------------------------------------------------------
# Neo4j part
# ---------------------------------------------------------------------------


@contextmanager
def _use(session: Any | None) -> Iterator[Any]:
    """The caller's session, or a fresh one that is closed afterwards."""
    if session is not None:
        yield session
        return
    from schemagraph.graph import driver  # imported here so tests need no neo4j server

    with driver.session() as s:
        yield s


def _batches(rows: list, size: int) -> Iterable[list]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _write(session: Any, query: str, rows: list, batch_size: int) -> None:
    """Run one statement over the rows, one transaction per batch."""

    def work(tx: Any, batch: list) -> None:
        tx.run(query, rows=batch).consume()

    for batch in _batches(rows, batch_size):
        session.execute_write(work, batch)


def _read(session: Any, query: str, **params: Any) -> list[dict[str, Any]]:
    def work(tx: Any) -> list[dict[str, Any]]:
        return [record.data() for record in tx.run(query, **params)]

    return session.execute_read(work)


def create_constraints_and_indexes(session: Any | None = None) -> None:
    """Uniqueness on uid per label, plus lookup indexes. Safe to run repeatedly."""
    with _use(session) as s:
        for statement in CONSTRAINTS:
            s.run(statement).consume()


def clear_database(db_id: str, session: Any | None = None, *, batch_size: int = 5000) -> int:
    """Delete every node of one db_id (and their edges). Other databases are untouched.

    Returns the number of nodes deleted. Done in batches so a large graph does
    not need one huge transaction.
    """
    total = 0
    with _use(session) as s:
        while True:
            def work(tx: Any) -> int:
                record = tx.run(DELETE_BATCH, db_id=db_id, limit=batch_size).single()
                return record["deleted"] if record else 0

            deleted = s.execute_write(work)
            total += deleted
            if deleted == 0:
                return total


def ingest_metadata(
    meta: DatabaseMetadata,
    session: Any | None = None,
    *,
    rebuild: bool = False,
    batch_size: int = BATCH_SIZE,
) -> IngestReport:
    """Write the graph for one database. Idempotent; `rebuild=True` clears it first."""
    rows = build_rows(meta)  # validates before anything is written
    with _use(session) as s:
        create_constraints_and_indexes(s)
        if rebuild:
            removed = clear_database(meta.db_id, s)
            log.info("cleared %d existing nodes for %s", removed, meta.db_id)
        _write(s, MERGE_NODES["Schema"], rows.schemas, batch_size)
        _write(s, MERGE_NODES["Table"], rows.tables, batch_size)
        _write(s, MERGE_NODES["Column"], rows.columns, batch_size)
        _write(s, MERGE_CONTAINS, rows.contains, batch_size)
        _write(s, MERGE_HAS_COLUMN, rows.has_column, batch_size)
        _write(s, MERGE_REFERENCES, rows.references, batch_size)

    return IngestReport(
        db_id=meta.db_id,
        schemas=len(rows.schemas),
        tables=len(rows.tables),
        columns=len(rows.columns),
        contains_edges=len(rows.contains),
        has_column_edges=len(rows.has_column),
        references_edges=len(rows.references),
    )


def apply_weights(db_id: str, session: Any | None = None, *, batch_size: int = BATCH_SIZE) -> int:
    """Recompute weight_forward / weight_reverse on stored edges from confidence and
    cardinality. Ingest already writes them; use this after changing the formula
    in weights.py so the graph does not need a full rebuild. Returns edges updated.
    """
    with _use(session) as s:
        edges = _read(s, READ_REFERENCES, db_id=db_id)
        rows = []
        for e in edges:
            forward, reverse = edge_weights(e["confidence"], e["cardinality"])
            rows.append({"src": e["src"], "dst": e["dst"], "weight_forward": forward, "weight_reverse": reverse})
        _write(s, SET_WEIGHTS, rows, batch_size)
    return len(rows)


def fetch_actual(db_id: str, session: Any) -> dict[str, Any]:
    pairs = lambda rows: {(r["src"], r["dst"]) for r in rows}  # noqa: E731
    return {
        "schemas": {r["uid"] for r in _read(session, READ_UIDS["Schema"], db_id=db_id)},
        "tables": {r["uid"] for r in _read(session, READ_UIDS["Table"], db_id=db_id)},
        "columns": {r["uid"] for r in _read(session, READ_UIDS["Column"], db_id=db_id)},
        "contains": pairs(_read(session, READ_CONTAINS, db_id=db_id)),
        "has_column": pairs(_read(session, READ_HAS_COLUMN, db_id=db_id)),
        "references": {(r["src"], r["dst"]): r for r in _read(session, READ_REFERENCES, db_id=db_id)},
    }


def verify_against_metadata(meta: DatabaseMetadata, session: Any | None = None) -> list[str]:
    """Human-readable differences between the graph and the metadata (empty list = OK)."""
    rows = build_rows(meta)
    with _use(session) as s:
        return compare(rows, fetch_actual(meta.db_id, s))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load a database's metadata JSON into Neo4j.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--meta", type=Path, help="metadata JSON (default: data/metadata/<db>.json)")
    parser.add_argument("--rebuild", action="store_true", help="delete this db_id's nodes first")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from schemagraph.graph import driver

    meta = extract.load_metadata(args.meta or extract.default_metadata_path(args.db))
    if meta.db_id != args.db:
        parser.error(f"{args.meta or 'metadata file'} is for db_id {meta.db_id!r}, not {args.db!r}")
    try:
        driver.check_connection()
        report = ingest_metadata(meta, rebuild=args.rebuild)
        problems = verify_against_metadata(meta)
    finally:
        driver.close_driver()

    print(
        f"{report.db_id}: {report.schemas} schemas, {report.tables} tables, {report.columns} columns; "
        f"edges: {report.contains_edges} CONTAINS, {report.has_column_edges} HAS_COLUMN, "
        f"{report.references_edges} REFERENCES"
    )
    if problems:
        print("VERIFY FAILED:")
        for p in problems:
            print(f"  {p}")
        return 1
    print("verified: the graph matches the metadata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())