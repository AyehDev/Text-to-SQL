"""graph/ingest.py without a Neo4j server: the pure row builder, the comparison, and the
Neo4j-facing functions driven through a fake session that records what it is asked to run."""

import json
from pathlib import Path

import pytest

from schemagraph.extraction import extract
from schemagraph.graph import ingest as ing

SAMPLE = Path(__file__).parent.parent / "contracts" / "examples" / "metadata.adventureworks.sample.json"
DB = "adventureworks"


@pytest.fixture(scope="module")
def meta():
    return extract.load_metadata(SAMPLE)


@pytest.fixture(scope="module")
def rows(meta):
    return ing.build_rows(meta)


# --- the sample file itself ---------------------------------------------------


def test_sample_file_is_consistent(meta):
    assert meta.check_integrity() == []
    assert (len(meta.tables), len(meta.columns), len(meta.relationships)) == (7, 93, 5)
    assert {r.source for r in meta.relationships} == {"declared", "inferred"}


# --- build_rows ---------------------------------------------------------------


def test_row_counts(rows, meta):
    assert len(rows.schemas) == 3
    assert len(rows.tables) == 7 and len(rows.columns) == 93
    assert len(rows.contains) == 7           # one per table
    assert len(rows.has_column) == 93        # one per column
    assert len(rows.references) == 5


def test_schema_uid_is_db_and_schema(rows):
    assert {r["uid"] for r in rows.schemas} == {f"{DB}:person", f"{DB}:production", f"{DB}:sales"}


def test_every_node_carries_db_id(rows):
    assert all(r["db_id"] == DB for r in rows.schemas + rows.tables + rows.columns)


def test_edges_point_at_existing_nodes(rows):
    schemas = {r["uid"] for r in rows.schemas}
    tables = {r["uid"] for r in rows.tables}
    columns = {r["uid"] for r in rows.columns}
    assert all(r["src"] in schemas and r["dst"] in tables for r in rows.contains)
    assert all(r["src"] in tables and r["dst"] in columns for r in rows.has_column)
    assert all(r["src"] in columns and r["dst"] in columns for r in rows.references)


def test_node_properties_are_neo4j_storable(rows):
    # Neo4j properties may be primitives or lists of primitives, never maps
    for r in rows.schemas + rows.tables + rows.columns:
        for key, value in r.items():
            assert not isinstance(value, dict), key
    for r in rows.references:
        assert all(not isinstance(v, dict) for v in r["props"].values())


def test_primary_key_and_sample_values_stay_lists(rows):
    order = next(t for t in rows.tables if t["name"] == "salesorderheader")
    assert order["primary_key"] == ["salesorderid"]  # column names, in key order
    ids = next(c for c in rows.columns if c["name"] == "salesorderid" and "header" in c["uid"])
    assert isinstance(ids["sample_values"], list)


def test_declared_edge_properties_and_weights(rows):
    e = next(r for r in rows.references if r["src"].endswith("salesorderheader.customerid"))
    p = e["props"]
    assert (p["source"], p["confidence"], p["cardinality"]) == ("declared", 1.0, "N:1")
    assert (p["weight_forward"], p["weight_reverse"]) == (1.0, 2.0)
    assert p["evidence_json"] is None and p["constraint_name"]
    assert p["from_table"] == f"{DB}:sales.salesorderheader" and p["to_table"] == f"{DB}:sales.customer"


def test_inferred_edge_keeps_its_evidence_as_json_text(rows):
    e = next(r for r in rows.references if r["props"]["source"] == "inferred")
    p = e["props"]
    assert p["constraint_name"] is None
    evidence = json.loads(p["evidence_json"])
    assert set(evidence) == {"name_match", "type_compatible", "value_overlap"}
    assert p["weight_forward"] > 1.0                      # costlier than a declared edge
    assert p["weight_reverse"] == pytest.approx(p["weight_forward"] + 1.0)


def test_inconsistent_metadata_is_refused(meta):
    broken = meta.model_copy(update={"relationships": meta.relationships + [
        meta.relationships[0].model_copy(update={"to_column": "adventureworks:no.such.column"})]})
    with pytest.raises(ValueError, match="inconsistent"):
        ing.build_rows(broken)


# --- a fake session -------------------------------------------------------------


class FakeResult:
    def __init__(self, data=None):
        self._data = data or []

    def consume(self):
        return None

    def single(self):
        return self._data[0] if self._data else None

    def __iter__(self):
        return iter(self._data)


class FakeRecord(dict):
    def data(self):
        return dict(self)


class FakeSession:
    """Records every statement; `reads` maps a query to the records it should return."""

    def __init__(self, reads=None, deletes=()):
        self.log: list[tuple[str, dict]] = []
        self.reads = reads or {}
        self.deletes = list(deletes)

    def run(self, query, **params):          # session-level (constraints)
        self.log.append((query, params))
        return FakeResult()

    def tx_run(self, query, **params):
        self.log.append((query, params))
        if query == ing.DELETE_BATCH:
            return FakeResult([FakeRecord(deleted=self.deletes.pop(0) if self.deletes else 0)])
        return FakeResult([FakeRecord(r) for r in self.reads.get(query, [])])

    def execute_write(self, fn, *args):
        return fn(_Tx(self), *args)

    execute_read = execute_write


class _Tx:
    def __init__(self, s):
        self.s = s

    def run(self, query, **params):
        return self.s.tx_run(query, **params)


def statements(session, query):
    return [params for q, params in session.log if q == query]


# --- ingest through the fake ----------------------------------------------------


def test_ingest_creates_constraints_then_writes_in_order(meta):
    s = FakeSession()
    report = ing.ingest_metadata(meta, s)
    queries = [q for q, _ in s.log]
    assert queries[: len(ing.CONSTRAINTS)] == ing.CONSTRAINTS
    written = [q for q in queries[len(ing.CONSTRAINTS):]]
    assert written == [
        ing.MERGE_NODES["Schema"], ing.MERGE_NODES["Table"], ing.MERGE_NODES["Column"],
        ing.MERGE_CONTAINS, ing.MERGE_HAS_COLUMN, ing.MERGE_REFERENCES,
    ]
    assert (report.schemas, report.tables, report.columns) == (3, 7, 93)
    assert (report.contains_edges, report.has_column_edges, report.references_edges) == (7, 93, 5)


def test_ingest_batches_large_inputs(meta):
    s = FakeSession()
    ing.ingest_metadata(meta, s, batch_size=40)
    col_batches = statements(s, ing.MERGE_NODES["Column"])
    assert [len(b["rows"]) for b in col_batches] == [40, 40, 13]


def test_ingest_is_all_merges(meta):
    # re-running must not duplicate anything: every write statement is a MERGE
    for q in [*ing.MERGE_NODES.values(), ing.MERGE_CONTAINS, ing.MERGE_HAS_COLUMN, ing.MERGE_REFERENCES]:
        assert "MERGE" in q and "CREATE " not in q


def test_rebuild_clears_before_writing(meta):
    s = FakeSession(deletes=[5, 0])
    ing.ingest_metadata(meta, s, rebuild=True)
    queries = [q for q, _ in s.log]
    first_write = queries.index(ing.MERGE_NODES["Schema"])
    assert ing.DELETE_BATCH in queries[:first_write]


def test_no_rebuild_never_deletes(meta):
    s = FakeSession()
    ing.ingest_metadata(meta, s)
    assert ing.DELETE_BATCH not in [q for q, _ in s.log]


def test_nothing_is_written_when_metadata_is_inconsistent(meta):
    s = FakeSession()
    broken = meta.model_copy(update={"columns": meta.columns[:-1] + [
        meta.columns[-1].model_copy(update={"table_uid": "adventureworks:x.y"})]})
    with pytest.raises(ValueError):
        ing.ingest_metadata(broken, s)
    assert s.log == []


def test_clear_database_loops_until_nothing_is_left():
    s = FakeSession(deletes=[5000, 5000, 120, 0])
    assert ing.clear_database("adventureworks", s, batch_size=5000) == 10120
    calls = statements(s, ing.DELETE_BATCH)
    assert len(calls) == 4 and all(c == {"db_id": "adventureworks", "limit": 5000} for c in calls)


def test_clear_database_is_scoped_to_one_db_id():
    assert "{db_id: $db_id}" in ing.DELETE_BATCH


# --- verify -----------------------------------------------------------------------


def actual_from(rows):
    return {
        "schemas": {r["uid"] for r in rows.schemas},
        "tables": {r["uid"] for r in rows.tables},
        "columns": {r["uid"] for r in rows.columns},
        "contains": {(r["src"], r["dst"]) for r in rows.contains},
        "has_column": {(r["src"], r["dst"]) for r in rows.has_column},
        "references": {(r["src"], r["dst"]): dict(r["props"]) for r in rows.references},
    }


def test_compare_matching_graph_has_no_problems(rows):
    assert ing.compare(rows, actual_from(rows)) == []


def test_compare_reports_missing_and_unexpected(rows):
    a = actual_from(rows)
    gone = sorted(a["tables"])[0]
    a["tables"].discard(gone)
    a["columns"].add("adventureworks:ghost.table.col")
    problems = ing.compare(rows, a)
    assert any("1 tables missing from graph" in p and gone in p for p in problems)
    assert any("1 columns unexpected in graph" in p for p in problems)


def test_compare_reports_a_wrong_edge_property(rows):
    a = actual_from(rows)
    pair = next(iter(a["references"]))
    a["references"][pair]["weight_forward"] = 99.0
    problems = ing.compare(rows, a)
    assert len(problems) == 1 and "weight_forward is 99.0" in problems[0]


def test_verify_reads_only_the_requested_db_id(meta, rows):
    reads = {
        ing.READ_UIDS["Schema"]: [{"uid": r["uid"]} for r in rows.schemas],
        ing.READ_UIDS["Table"]: [{"uid": r["uid"]} for r in rows.tables],
        ing.READ_UIDS["Column"]: [{"uid": r["uid"]} for r in rows.columns],
        ing.READ_CONTAINS: [{"src": r["src"], "dst": r["dst"]} for r in rows.contains],
        ing.READ_HAS_COLUMN: [{"src": r["src"], "dst": r["dst"]} for r in rows.has_column],
        ing.READ_REFERENCES: [
            {"src": r["src"], "dst": r["dst"], **{k: r["props"][k] for k in
             ("source", "confidence", "cardinality", "weight_forward", "weight_reverse")}}
            for r in rows.references
        ],
    }
    s = FakeSession(reads=reads)
    assert ing.verify_against_metadata(meta, s) == []
    assert all(params == {"db_id": "adventureworks"} for _, params in s.log)
    s2 = FakeSession(reads={**reads, ing.READ_UIDS["Table"]: []})
    assert any("tables missing from graph" in p for p in ing.verify_against_metadata(meta, s2))


# --- apply_weights ----------------------------------------------------------------


def test_apply_weights_recomputes_from_stored_confidence_and_cardinality():
    reads = {ing.READ_REFERENCES: [
        {"src": "a", "dst": "b", "confidence": 1.0, "cardinality": "N:1"},
        {"src": "c", "dst": "d", "confidence": 0.72, "cardinality": "N:1"},
        {"src": "e", "dst": "f", "confidence": 1.0, "cardinality": "1:1"},
    ]}
    s = FakeSession(reads=reads)
    assert ing.apply_weights("adventureworks", s) == 3
    (batch,) = statements(s, ing.SET_WEIGHTS)
    got = {(r["src"], r["dst"]): (r["weight_forward"], r["weight_reverse"]) for r in batch["rows"]}
    assert got == {("a", "b"): (1.0, 2.0), ("c", "d"): (2.4, 3.4), ("e", "f"): (1.0, 1.0)}


def test_reference_edges_carry_their_properties():
    # without this SET the edge would exist but have no weights, confidence or evidence
    assert "SET r += row.props" in ing.MERGE_REFERENCES
