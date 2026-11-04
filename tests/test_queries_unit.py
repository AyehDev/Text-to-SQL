"""graph/queries.py without Neo4j. A fake store answers each query from what `ingest`
would have written, so these tests also prove that ingest -> queries round-trips."""

from pathlib import Path

import pytest

from schemagraph.extraction import extract
from schemagraph.graph import ingest, queries as q
from schemagraph.models import RelationshipMeta

SAMPLE = Path(__file__).parent.parent / "contracts" / "examples" / "metadata.adventureworks.sample.json"
DB = "adventureworks"
CUSTOMER = f"{DB}:sales.customer"
HEADER = f"{DB}:sales.salesorderheader"
PERSON = f"{DB}:person.person"


@pytest.fixture(scope="module")
def meta():
    return extract.load_metadata(SAMPLE)


def stored(props):
    """What Neo4j keeps: properties set to null are dropped."""
    return {k: v for k, v in props.items() if v is not None}


class FakeRecord(dict):
    def data(self):
        return dict(self)


class FakeStore:
    """Answers the queries in queries.py from the rows `ingest` builds for one db_id."""

    def __init__(self, meta):
        self.db_id = meta.db_id
        self.rows = ingest.build_rows(meta)
        self.calls: list[tuple[str, dict]] = []

    def execute_read(self, fn):
        return fn(self)

    def run(self, query, **p):
        self.calls.append((query, p))
        if p.get("db_id") != self.db_id:
            return []
        r = self.rows
        wanted = lambda uid: p.get("all") or uid in p.get("uids", ())  # noqa: E731
        if query == q.READ_TABLES:
            return [FakeRecord(props=stored(t)) for t in r.tables if wanted(t["uid"])]
        if query == q.READ_COLUMNS:
            cols = sorted((c for c in r.columns if wanted(c["table_uid"])), key=lambda c: (c["table_uid"], c["ordinal_position"]))
            return [FakeRecord(props=stored(c)) for c in cols]
        table_of = {c["uid"]: c["table_uid"] for c in r.columns}
        edge = lambda e: FakeRecord(src=e["src"], dst=e["dst"], props=stored(e["props"]))  # noqa: E731
        if query == q.READ_OUTGOING:
            return [edge(e) for e in sorted(r.references, key=lambda e: (e["src"], e["dst"])) if wanted(table_of[e["src"]])]
        if query == q.READ_INCOMING:
            return [edge(e) for e in sorted(r.references, key=lambda e: (e["src"], e["dst"])) if table_of[e["dst"]] == p["uid"]]
        if query == q.READ_EDGES:
            return [edge(e) for e in sorted(r.references, key=lambda e: (e["src"], e["dst"]))
                    if p["inferred"] or e["props"]["source"] == "declared"]
        if query == q.READ_SUMMARIES:
            out = []
            for t in sorted(r.tables, key=lambda t: t["uid"]):
                n = sum(c["table_uid"] == t["uid"] for c in r.columns)
                out.append(FakeRecord(uid=t["uid"], description=t["description"], column_count=n))
            return [FakeRecord({k: v for k, v in o.items() if v is not None}) for o in out]
        raise AssertionError(f"unexpected query: {query}")


@pytest.fixture
def store(meta):
    return FakeStore(meta)


# --- round trip ------------------------------------------------------------------


def test_get_all_tables_round_trips_the_metadata(meta, store):
    details = q.get_all_tables(DB, store)
    assert [d.table.uid for d in details] == sorted(t.uid for t in meta.tables)
    for d in details:
        assert d.table == next(t for t in meta.tables if t.uid == d.table.uid)
        expected_cols = sorted((c for c in meta.columns if c.table_uid == d.table.uid), key=lambda c: c.ordinal_position)
        assert d.columns == expected_cols
        col_uids = {c.uid for c in expected_cols}
        expected_refs = sorted((r for r in meta.relationships if r.from_column in col_uids), key=lambda r: (r.from_column, r.to_column))
        assert d.references == expected_refs


def test_inferred_relationship_keeps_its_evidence(store):
    details = {d.table.uid: d for d in q.get_all_tables(DB, store)}
    (rel,) = details[f"{DB}:production.transactionhistoryarchive"].references
    assert rel.source == "inferred" and rel.constraint_name is None
    assert rel.evidence is not None and rel.evidence.name_match == "exact"


def test_declared_relationship_has_no_evidence(store):
    (rel,) = {d.table.uid: d for d in q.get_all_tables(DB, store)}[HEADER].references
    assert rel.source == "declared" and rel.evidence is None and rel.constraint_name


def test_all_references_are_valid_models(store):
    for d in q.get_all_tables(DB, store):
        assert all(isinstance(r, RelationshipMeta) for r in d.references)


# --- get_tables --------------------------------------------------------------------


def test_get_tables_keeps_the_requested_order(store):
    got = q.get_tables(DB, [PERSON, HEADER, CUSTOMER], store)
    assert [d.table.uid for d in got] == [PERSON, HEADER, CUSTOMER]


def test_get_tables_skips_unknown_uids_and_duplicates(store):
    got = q.get_tables(DB, [HEADER, f"{DB}:no.such", HEADER], store)
    assert [d.table.uid for d in got] == [HEADER]


def test_get_tables_with_no_uids_asks_nothing(store):
    assert q.get_tables(DB, [], store) == []
    assert store.calls == []


def test_get_tables_returns_only_requested_tables_columns_and_edges(store):
    (d,) = q.get_tables(DB, [CUSTOMER], store)
    assert {c.table_uid for c in d.columns} == {CUSTOMER}
    assert [r.to_column for r in d.references] == [f"{PERSON}.businessentityid"]


# --- list_tables -------------------------------------------------------------------


def test_list_tables(meta, store):
    got = q.list_tables(DB, store)
    assert len(got) == 7 and [t.uid for t in got] == sorted(t.uid for t in meta.tables)
    header = next(t for t in got if t.uid == HEADER)
    assert header.display_name == "sales.salesorderheader"
    assert header.column_count == sum(c.table_uid == HEADER for c in meta.columns)
    assert sum(t.column_count for t in got) == 93


# --- get_table_detail --------------------------------------------------------------


def test_table_detail_has_both_directions(store):
    d = q.get_table_detail(DB, CUSTOMER, store)
    assert [r.to_column for r in d.references] == [f"{PERSON}.businessentityid"]
    assert [r.from_column for r in d.referenced_by] == [f"{HEADER}.customerid"]


def test_table_with_no_incoming_edges(store):
    assert q.get_table_detail(DB, HEADER, store).referenced_by[0].from_column.startswith(f"{DB}:sales.salesorderdetail")
    assert q.get_table_detail(DB, f"{DB}:sales.salesorderdetail", store).referenced_by == []


def test_unknown_table_raises_not_found(store):
    with pytest.raises(q.TableNotFound):
        q.get_table_detail(DB, f"{DB}:no.such", store)


# --- reference edges for the planner ----------------------------------------------


def test_reference_edges(store):
    edges = q.get_reference_edges(DB, session=store)
    assert len(edges) == 5
    e = next(e for e in edges if e.from_column == f"{HEADER}.customerid")
    assert (e.from_table, e.to_table) == (HEADER, CUSTOMER)
    assert (e.source, e.confidence, e.cardinality) == ("declared", 1.0, "N:1")
    assert (e.weight_forward, e.weight_reverse) == (1.0, 2.0)


def test_reference_edges_can_leave_out_inferred(store):
    declared = q.get_reference_edges(DB, include_inferred=False, session=store)
    assert len(declared) == 4 and {e.source for e in declared} == {"declared"}
    inferred = [e for e in q.get_reference_edges(DB, session=store) if e.source == "inferred"]
    assert len(inferred) == 1 and inferred[0].constraint_name is None and inferred[0].weight_forward > 1.0


# --- scoping and Cypher shape --------------------------------------------------------


def test_another_db_id_sees_nothing(store):
    assert q.get_all_tables("other", store) == []
    assert q.list_tables("other", store) == []
    assert q.get_reference_edges("other", session=store) == []
    with pytest.raises(q.TableNotFound):
        q.get_table_detail("other", CUSTOMER, store)


def test_every_query_is_scoped_to_one_db_id():
    for text in (q.READ_TABLES, q.READ_COLUMNS, q.READ_OUTGOING, q.READ_INCOMING, q.READ_SUMMARIES, q.READ_EDGES):
        assert "db_id: $db_id" in text


def test_queries_never_interpolate_values():
    for text in (q.READ_TABLES, q.READ_COLUMNS, q.READ_OUTGOING, q.READ_INCOMING, q.READ_SUMMARIES, q.READ_EDGES):
        assert "{" not in text.replace("{db_id: $db_id}", "").replace("{table_uid: $uid}", "").replace("{.*}", "")


def test_queries_are_read_only():
    for text in (q.READ_TABLES, q.READ_COLUMNS, q.READ_OUTGOING, q.READ_INCOMING, q.READ_SUMMARIES, q.READ_EDGES):
        assert not any(w in text.upper() for w in ("MERGE", "CREATE", "DELETE", " SET "))


# --- pure assembly --------------------------------------------------------------------


def test_missing_optional_properties_default(meta):
    props = {"uid": "d:s.t", "schema": "s", "name": "t"}  # neo4j dropped every null
    t = q.table_from_props(props)
    assert (t.description, t.row_count, t.primary_key) == (None, None, [])
    c = q.column_from_props({"uid": "d:s.t.c", "table_uid": "d:s.t", "name": "c", "data_type": "int",
                             "ordinal_position": 1, "is_nullable": True})
    assert (c.is_primary_key, c.is_unique, c.sample_values, c.description) == (False, False, [], None)


def test_columns_of_unlisted_tables_are_ignored():
    tables = [{"props": {"uid": "d:s.t", "schema": "s", "name": "t"}}]
    columns = [{"props": {"uid": "d:s.x.c", "table_uid": "d:s.x", "name": "c", "data_type": "int",
                          "ordinal_position": 1, "is_nullable": True}}]
    (d,) = q.assemble_details(tables, columns, [])
    assert d.columns == []
