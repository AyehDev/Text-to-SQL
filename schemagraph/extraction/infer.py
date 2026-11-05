"""Relationship inference: find foreign keys the database never declared (SPEC 5.4).

Declared foreign keys are only part of the picture, so this module proposes
more, as RelationshipMeta(source="inferred") with a confidence and the evidence
behind it. It works from the metadata object plus one cheap query per candidate.

The pipeline, cheapest filter first:

  1. targets    columns that are unique on their own (primary or unique key),
                except generic names such as rowguid / modifieddate / name
  2. sources    any column of another table in the same type family that is NOT
                already the source of a relationship (declared or inferred)
  3. name       the source's name must point at the target (see `name_signal`)
  4. direction  when both ends are unique (a shared-key, 1:1 link) the names
                must match exactly, and the key points from the smaller table
                to the larger (so no pair of 1:1 tables gets two arrows)
  5. overlap    (database) at least `min_overlap` of the source's distinct
                values must exist in the target
  6. one target per source column: best name signal, then "tightest parent"
                (highest Jaccard overlap), then the most-referenced table
  7. confidence = 0.4 * name score + 0.6 * overlap, capped below 1.0 so an
                inferred edge never weighs as much as a declared one

Steps 1-4 (`find_candidate_pairs`) are pure and unit tested; step 5 is the only
one that reads data, and `infer_relationships` accepts a replacement for it.
Only single-column relationships are inferred.
"""

from __future__ import annotations

import argparse
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import psycopg
from psycopg import sql as pgsql

from schemagraph import db
from schemagraph.extraction import extract
from schemagraph.models import (
    CandidatePair,
    ColumnMeta,
    DatabaseMetadata,
    InferenceEvidence,
    RelationshipMeta,
)

log = logging.getLogger(__name__)

# Normalized (lowercase letters and digits only) names that identify nothing.
GENERIC_TARGET_NAMES = frozenset({"rowguid", "modifieddate", "name"})
# Too short to match by name; such a target can only be reached through the
# table-name pattern (customer_id -> customer.id).
WEAK_NAMES = frozenset({"id"})
MIN_SUFFIX_LEN = 4
MIN_TABLE_NAME_LEN = 3

NAME_RANK = {"exact": 3, "suffix": 2, "table_name": 1}
NAME_SCORE = {"exact": 1.0, "suffix": 0.85, "table_name": 0.7}
NAME_WEIGHT, OVERLAP_WEIGHT = 0.4, 0.6
# Below 1.0 on purpose: the planner prices an edge as 1 + 5 * (1 - confidence),
# so confidence 1.0 would make an inferred join cost the same as a declared one.
MAX_INFERRED_CONFIDENCE = 0.95

MIN_OVERLAP = 0.9
# Bound the work on large tables: look at a prefix of the source column, and at
# most this many distinct values of it.
OVERLAP_SCAN_ROWS = 100_000
OVERLAP_MAX_DISTINCT = 10_000


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------


def type_family(data_type: str) -> str:
    """Coarse type class used to decide whether two columns can hold the same keys.

    Native type strings differ between databases and in modifiers
    ("character varying(50)"), so compare families, not strings.
    """
    base = data_type.split("(")[0].strip().lower()
    if base.endswith("[]"):
        return "array:" + type_family(base[:-2])
    if base in {"smallint", "integer", "bigint", "int2", "int4", "int8"}:
        return "int"
    if base in {"numeric", "decimal", "real", "double precision", "money"}:
        return "numeric"
    if base in {"text", "character varying", "character", "bpchar", "varchar", "char", "name", "citext"}:
        return "text"
    if base == "date" or base.startswith(("timestamp", "time")):
        return "time"
    return base


def _norm(name: str) -> str:
    """Lowercase letters and digits only, so CustomerID, customer_id and customerid agree."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def name_signal(from_name: str, to_name: str, to_table: str) -> str | None:
    """How the source column's name points at the target, or None.

    exact       same name                          customerid -> customerid
    suffix      source ends with the target name   billtoaddressid -> addressid
    table_name  source ends with <table>id         salespersonid -> salesperson.businessentityid

    Deliberately strict: a loose "table name appears somewhere inside" rule
    matched productcategoryid -> product.productid, which is not a relationship.
    """
    nf, nt, tab = _norm(from_name), _norm(to_name), _norm(to_table)
    if not nf or not nt:
        return None
    if nt not in WEAK_NAMES:
        if nf == nt:
            return "exact"
        if len(nt) >= MIN_SUFFIX_LEN and nf.endswith(nt):
            return "suffix"
    if len(tab) >= MIN_TABLE_NAME_LEN and nf.endswith(tab + "id"):
        return "table_name"
    return None


def _direction_ok(source: ColumnMeta, target: ColumnMeta, row_counts: dict[str, int | None]) -> bool:
    """For a unique-to-unique pair, allow only child -> parent (smaller -> larger).

    Equal sizes are ambiguous (an extension table with the same keys), so keep
    exactly one direction, chosen by UID, instead of a pair of opposite arrows.
    """
    rs, rt = row_counts.get(source.table_uid), row_counts.get(target.table_uid)
    if rs is None or rt is None:
        return True
    if rs != rt:
        return rs < rt
    return source.uid < target.uid


def find_candidate_pairs(meta: DatabaseMetadata) -> list[CandidatePair]:
    """Steps 1-4: pairs worth checking against the data. Pure, no database."""
    tables = {t.uid: t for t in meta.tables}
    row_counts = {t.uid: t.row_count for t in meta.tables}
    has_relationship = {r.from_column for r in meta.relationships}

    targets = [
        c for c in meta.columns if c.is_unique and _norm(c.name) not in GENERIC_TARGET_NAMES
    ]
    by_family: dict[str, list[ColumnMeta]] = defaultdict(list)
    for c in meta.columns:
        by_family[type_family(c.data_type)].append(c)

    pairs = []
    for target in targets:
        target_table = tables[target.table_uid].name
        for source in by_family[type_family(target.data_type)]:
            if (
                source.uid == target.uid
                or source.table_uid == target.table_uid
                or source.uid in has_relationship
            ):
                continue
            signal = name_signal(source.name, target.name, target_table)
            if signal is None:
                continue
            if source.is_unique and (
                # A key pointing at another key is a shared-key (1:1) link, and
                # those keep the same column name. A looser name match between
                # two sequential ids is coincidence (productreviewid ~ reviewid).
                signal != "exact"
                or not _direction_ok(source, target, row_counts)
            ):
                continue
            pairs.append(
                CandidatePair(
                    from_column=source.uid,
                    to_column=target.uid,
                    name_match=signal,
                    type_compatible=True,
                )
            )
    pairs.sort(key=lambda p: (p.from_column, p.to_column))
    return pairs


def confidence(name_match: str, overlap: float) -> float:
    score = NAME_WEIGHT * NAME_SCORE[name_match] + OVERLAP_WEIGHT * overlap
    return round(min(score, MAX_INFERRED_CONFIDENCE), 2)


# ---------------------------------------------------------------------------
# Value overlap (the one step that reads data)
# ---------------------------------------------------------------------------


class OverlapCounts(NamedTuple):
    n: int  # distinct non-null source values examined
    hit: int  # how many of them exist in the target column


def overlap_counts(db_id: str, source: extract.ColumnRef, target: extract.ColumnRef) -> OverlapCounts:
    """Count how many distinct source values appear in the target column."""
    query = pgsql.SQL(
        """
        SELECT count(*) AS n,
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM {tt} t WHERE t.{tc} = s.v)) AS hit
        FROM (
            SELECT v FROM (SELECT {sc} AS v FROM {st} WHERE {sc} IS NOT NULL LIMIT {scan}) p
            GROUP BY v LIMIT {distinct}
        ) s
        """
    ).format(
        sc=pgsql.Identifier(source.column),
        st=pgsql.Identifier(source.schema, source.table),
        tc=pgsql.Identifier(target.column),
        tt=pgsql.Identifier(target.schema, target.table),
        scan=pgsql.Literal(OVERLAP_SCAN_ROWS),
        distinct=pgsql.Literal(OVERLAP_MAX_DISTINCT),
    )
    try:
        row = db.run_admin_query(db_id, query)[0]
    except psycopg.Error as exc:
        # An incomparable pair (or unreadable column) is "no evidence", not a crash.
        log.warning(
            "overlap check failed for %s.%s.%s -> %s.%s.%s: %s",
            source.schema, source.table, source.column,
            target.schema, target.table, target.column, exc,
        )
        return OverlapCounts(0, 0)
    return OverlapCounts(int(row["n"]), int(row["hit"]))


def value_overlap(db_id: str, from_column: str, to_column: str) -> float:
    """Share of the source column's distinct values that exist in the target (0 to 1).

    An empty source column has no evidence, so it scores 0.0.
    """
    counts = overlap_counts(
        db_id, extract.resolve_column(db_id, from_column), extract.resolve_column(db_id, to_column)
    )
    return counts.hit / counts.n if counts.n else 0.0


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------


def infer_relationships(
    db_id: str,
    meta: DatabaseMetadata,
    min_confidence: float = 0.5,
    *,
    min_overlap: float = MIN_OVERLAP,
    overlap_fn: Callable[[str, str], OverlapCounts] | None = None,
) -> list[RelationshipMeta]:
    """Inferred relationships for `meta`, at most one per source column.

    Columns that already have a relationship in `meta` are left alone, so
    calling this twice on the same object adds nothing new. `overlap_fn`
    (source UID, target UID) -> OverlapCounts replaces the database query; tests
    and the recovery harness use it.
    """
    cols = {c.uid: c for c in meta.columns}
    tables = {t.uid: t for t in meta.tables}

    if overlap_fn is None:
        refs: dict[str, extract.ColumnRef] = {}
        for c in meta.columns:
            t = tables[c.table_uid]
            refs[c.uid] = extract.ColumnRef(t.schema_, t.name, c.name, c.data_type)

        def overlap_fn(source_uid: str, target_uid: str) -> OverlapCounts:
            return overlap_counts(db_id, refs[source_uid], refs[target_uid])

    incoming = Counter(cols[r.to_column].table_uid for r in meta.relationships)

    best: dict[str, tuple[tuple, CandidatePair, float]] = {}
    for pair in find_candidate_pairs(meta):  # sorted, so ties keep the lowest UID
        counts = overlap_fn(pair.from_column, pair.to_column)
        if counts.n == 0:
            continue
        overlap = counts.hit / counts.n
        if overlap < min_overlap:
            continue
        target = cols[pair.to_column]
        # A unique column's distinct count is about its table's row count.
        n_target = max(tables[target.table_uid].row_count or 0, counts.hit)
        tightness = counts.hit / (counts.n + n_target - counts.hit)
        key = (NAME_RANK[pair.name_match], round(tightness, 3), incoming[target.table_uid])
        if pair.from_column not in best or key > best[pair.from_column][0]:
            best[pair.from_column] = (key, pair, overlap)

    inferred = []
    for from_uid, (_, pair, overlap) in best.items():
        conf = confidence(pair.name_match, overlap)
        if conf < min_confidence:
            continue
        inferred.append(
            RelationshipMeta(
                from_column=from_uid,
                to_column=pair.to_column,
                source="inferred",
                confidence=conf,
                cardinality="1:1" if cols[from_uid].is_unique else "N:1",
                constraint_name=None,
                evidence=InferenceEvidence(
                    name_match=pair.name_match,
                    type_compatible=True,
                    value_overlap=round(overlap, 4),
                ),
            )
        )
    inferred.sort(key=lambda r: (r.from_column, r.to_column))
    return inferred


def with_inferred(db_id: str, meta: DatabaseMetadata, **kwargs) -> DatabaseMetadata:
    """A copy of `meta` whose inferred relationships are recomputed from scratch.

    Existing inferred relationships are dropped first, so the result does not
    depend on how often this was run before.
    """
    declared_only = meta.model_copy(
        update={"relationships": [r for r in meta.relationships if r.source == "declared"]}
    )
    inferred = infer_relationships(db_id, declared_only, **kwargs)
    merged = sorted(
        declared_only.relationships + inferred,
        key=lambda r: (r.from_column, r.to_column, r.constraint_name or ""),
    )
    result = declared_only.model_copy(update={"relationships": merged})
    problems = result.check_integrity()
    if problems:
        raise extract.ExtractionError(f"inference produced inconsistent metadata: {problems[:5]}")
    return result


# ---------------------------------------------------------------------------
# Command line:  python -m schemagraph.extraction.infer --db adventureworks
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add inferred relationships to saved metadata.")
    parser.add_argument("--db", required=True, help="db_id")
    parser.add_argument("--meta", type=Path, help="metadata JSON (default: data/metadata/<db>.json)")
    parser.add_argument("--out", type=Path, help="where to write (default: overwrite --meta)")
    parser.add_argument("--min-overlap", type=float, default=MIN_OVERLAP)
    parser.add_argument("--min-confidence", type=float, default=0.5)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    source = args.meta or extract.default_metadata_path(args.db)
    meta = extract.load_metadata(source)
    result = with_inferred(
        args.db, meta, min_confidence=args.min_confidence, min_overlap=args.min_overlap
    )
    out = args.out or source
    extract.save_metadata(result, out)

    inferred = [r for r in result.relationships if r.source == "inferred"]
    print(f"{args.db}: {len(inferred)} inferred relationship(s) added -> {out}")
    for r in inferred:
        print(
            f"  {r.confidence:.2f}  {r.from_column.split(':', 1)[1]} -> {r.to_column.split(':', 1)[1]}"
            f"  [{r.evidence.name_match}, overlap {r.evidence.value_overlap:.2f}, {r.cardinality}]"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())