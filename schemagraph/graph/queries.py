"""Read the schema graph back out as the shared contract models (SPEC 5.8).

    get_tables(db_id, uids)            tables + columns (+ outgoing FKs), for building DDL
    get_all_tables(db_id)              the same for every table (full-schema mode)
    list_tables(db_id)                 one summary line per table  (/api/schema)
    get_table_detail(db_id, uid)       one table with FKs in both directions
    get_reference_edges(db_id)         every REFERENCES edge, for the join planner

What turns Neo4j properties into models is pure (`table_from_props`, ...) and is
tested by round-tripping what `ingest` writes. The Cypher is fixed text, with
values always passed as parameters.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from schemagraph.graph.cypher import read, use_session
from schemagraph.models import (
    ColumnMeta,
    InferenceEvidence,
    RelationshipMeta,
    TableDetail,
    TableMeta,
    TableSummary,
    TableWithRelationships,
    display_name,
)

log = logging.getLogger(__name__)


class TableNotFound(LookupError):
    """The table uid is not in the graph for this db_id (the API maps this to 404)."""


# ---------------------------------------------------------------------------
# Cypher. `$all` true means "every table"; otherwise only the uids listed.
# ---------------------------------------------------------------------------

READ_TABLES = "MATCH (t:Table {db_id: $db_id}) WHERE $all OR t.uid IN $uids RETURN t{.*} AS props"
READ_COLUMNS = (
    "MATCH (t:Table {db_id: $db_id})-[:HAS_COLUMN]->(c:Column) WHERE $all OR t.uid IN $uids "
    "RETURN c{.*} AS props ORDER BY c.table_uid, c.ordinal_position"
)
READ_OUTGOING = (
    "MATCH (a:Column {db_id: $db_id})-[r:REFERENCES]->(b:Column) WHERE $all OR a.table_uid IN $uids "
    "RETURN a.uid AS src, b.uid AS dst, r{.*} AS props ORDER BY a.uid, b.uid"
)
READ_INCOMING = (
    "MATCH (a:Column {db_id: $db_id})-[r:REFERENCES]->(b:Column {table_uid: $uid}) "
    "RETURN a.uid AS src, b.uid AS dst, r{.*} AS props ORDER BY a.uid, b.uid"
)
READ_SUMMARIES = (
    "MATCH (t:Table {db_id: $db_id}) OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column) "
    "RETURN t.uid AS uid, t.description AS description, count(c) AS column_count ORDER BY t.uid"
)
READ_EDGES = (
    "MATCH (a:Column {db_id: $db_id})-[r:REFERENCES]->(b:Column) "
    "WHERE $inferred OR r.source = 'declared' "
    "RETURN a.uid AS src, b.uid AS dst, r{.*} AS props ORDER BY a.uid, b.uid"
)

# ---------------------------------------------------------------------------
# Pure part: Neo4j properties -> models
#
# Neo4j drops properties that were set to null, so an absent key means None.
# `db_id` is stored on every node for scoping but is not part of the models.
# ---------------------------------------------------------------------------


def table_from_props(props: dict[str, Any]) -> TableMeta:
    return TableMeta(
        uid=props["uid"],
        schema=props["schema"],
        name=props["name"],
        description=props.get("description"),
        description_source=props.get("description_source"),
        row_count=props.get("row_count"),
        primary_key=list(props.get("primary_key") or []),
    )


def column_from_props(props: dict[str, Any]) -> ColumnMeta:
    return ColumnMeta(
        uid=props["uid"],
        table_uid=props["table_uid"],
        name=props["name"],
        data_type=props["data_type"],
        ordinal_position=props["ordinal_position"],
        is_nullable=props["is_nullable"],
        is_primary_key=props.get("is_primary_key", False),
        is_unique=props.get("is_unique", False),
        description=props.get("description"),
        description_source=props.get("description_source"),
        sample_values=list(props.get("sample_values") or []),
    )


def relationship_from_edge(src: str, dst: str, props: dict[str, Any]) -> RelationshipMeta:
    evidence_json = props.get("evidence_json")
    return RelationshipMeta(
        from_column=src,
        to_column=dst,
        source=props["source"],
        confidence=props["confidence"],
        cardinality=props["cardinality"],
        constraint_name=props.get("constraint_name"),
        evidence=InferenceEvidence.model_validate(json.loads(evidence_json)) if evidence_json else None,
    )


def assemble_details(
    tables: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    order: list[str] | None = None,
) -> list[TableDetail]:
    """Group columns and outgoing edges under their tables.

    `order` (a list of table uids) fixes the output order and drops uids that
    were not found; without it, tables come out sorted by uid.
    """
    by_uid = {t["props"]["uid"]: table_from_props(t["props"]) for t in tables}
    cols: dict[str, list[ColumnMeta]] = {uid: [] for uid in by_uid}
    for c in columns:
        col = column_from_props(c["props"])
        if col.table_uid in cols:
            cols[col.table_uid].append(col)
    refs: dict[str, list[RelationshipMeta]] = {uid: [] for uid in by_uid}
    table_of = {c.uid: c.table_uid for group in cols.values() for c in group}
    for e in edges:
        rel = relationship_from_edge(e["src"], e["dst"], e["props"])
        owner = table_of.get(rel.from_column)
        if owner in refs:
            refs[owner].append(rel)

    uids = [u for u in order if u in by_uid] if order is not None else sorted(by_uid)
    return [TableDetail(table=by_uid[u], columns=cols[u], references=refs[u]) for u in uids]


# ---------------------------------------------------------------------------
# Neo4j part
# ---------------------------------------------------------------------------


def _details(db_id: str, uids: list[str] | None, session: Any | None) -> list[TableDetail]:
    params = {"db_id": db_id, "all": uids is None, "uids": uids or []}
    with use_session(session) as s:
        tables = read(s, READ_TABLES, **params)
        columns = read(s, READ_COLUMNS, **params)
        edges = read(s, READ_OUTGOING, **params)
    details = assemble_details(tables, columns, edges, order=uids)
    if uids is not None and len(details) < len(set(uids)):
        missing = sorted(set(uids) - {d.table.uid for d in details})
        log.warning("get_tables: %d table uid(s) not in the graph for %s: %s", len(missing), db_id, missing[:5])
    return details


def get_tables(db_id: str, uids: list[str], session: Any | None = None) -> list[TableDetail]:
    """Tables with their columns and outgoing foreign keys, in the order of `uids`.

    Unknown uids are skipped (with a warning), so a stale retrieval result does
    not break prompt building.
    """
    if not uids:
        return []
    return _details(db_id, list(dict.fromkeys(uids)), session)


def get_all_tables(db_id: str, session: Any | None = None) -> list[TableDetail]:
    """Every table in the database, sorted by uid (full-schema mode)."""
    return _details(db_id, None, session)


def list_tables(db_id: str, session: Any | None = None) -> list[TableSummary]:
    with use_session(session) as s:
        rows = read(s, READ_SUMMARIES, db_id=db_id)
    return [
        TableSummary(
            uid=r["uid"],
            display_name=display_name(r["uid"]),
            description=r.get("description"),
            column_count=r["column_count"],
        )
        for r in rows
    ]


def get_table_detail(db_id: str, uid: str, session: Any | None = None) -> TableWithRelationships:
    """One table with its columns, the foreign keys it has, and the ones pointing at it."""
    with use_session(session) as s:
        (detail,) = _details(db_id, [uid], s) or [None]
        if detail is None:
            raise TableNotFound(f"table {uid!r} not found in the graph for {db_id!r}")
        incoming = read(s, READ_INCOMING, db_id=db_id, uid=uid)
    return TableWithRelationships(
        table=detail.table,
        columns=detail.columns,
        references=detail.references,
        referenced_by=[relationship_from_edge(e["src"], e["dst"], e["props"]) for e in incoming],
    )


@dataclass(frozen=True)
class ReferenceEdge:
    """One REFERENCES edge as the join planner needs it: both column and table ends, and costs."""

    from_column: str
    to_column: str
    from_table: str
    to_table: str
    source: str
    confidence: float
    cardinality: str
    weight_forward: float
    weight_reverse: float
    constraint_name: str | None = None


def get_reference_edges(
    db_id: str, *, include_inferred: bool = True, session: Any | None = None
) -> list[ReferenceEdge]:
    """Every relationship in the database; `include_inferred=False` keeps declared ones only."""
    with use_session(session) as s:
        rows = read(s, READ_EDGES, db_id=db_id, inferred=include_inferred)
    return [
        ReferenceEdge(
            from_column=r["src"],
            to_column=r["dst"],
            from_table=r["props"]["from_table"],
            to_table=r["props"]["to_table"],
            source=r["props"]["source"],
            confidence=r["props"]["confidence"],
            cardinality=r["props"]["cardinality"],
            weight_forward=r["props"]["weight_forward"],
            weight_reverse=r["props"]["weight_reverse"],
            constraint_name=r["props"].get("constraint_name"),
        )
        for r in rows
    ]