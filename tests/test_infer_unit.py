"""Unit tests for extraction/infer.py. No database: overlap is faked.

`meta_of` builds a small DatabaseMetadata from a compact description, and
`overlaps` builds the fake overlap function from {(source, target): (n, hit)}.
"""

import pytest

from schemagraph.extraction import infer
from schemagraph.extraction.infer import (
    OverlapCounts,
    confidence,
    find_candidate_pairs,
    infer_relationships,
    name_signal,
    type_family,
    with_inferred,
)
from schemagraph.models import (
    ColumnMeta,
    DatabaseMetadata,
    RelationshipMeta,
    SchemaMeta,
    TableMeta,
)

DB = "d"


def uid(table, column=None):
    return f"d:s.{table}" + (f".{column}" if column else "")


def meta_of(spec, declared=()):
    """spec: {table: (row_count, [(column, type, unique), ...])}; declared: [(from, to), ...]."""
    tables, columns = [], []
    for table, (rows, cols) in spec.items():
        tables.append(TableMeta(uid=uid(table), schema="s", name=table, row_count=rows))
        for i, (name, data_type, unique) in enumerate(cols, 1):
            columns.append(
                ColumnMeta(
                    uid=uid(table, name), table_uid=uid(table), name=name, data_type=data_type,
                    ordinal_position=i, is_nullable=not unique, is_primary_key=unique, is_unique=unique,
                )
            )
    rels = [
        RelationshipMeta(
            from_column=f, to_column=t, source="declared", confidence=1.0,
            cardinality="N:1", constraint_name=f"fk_{i}",
        )
        for i, (f, t) in enumerate(declared)
    ]
    return DatabaseMetadata(
        db_id=DB, extracted_at="2026-10-26T00:00:00Z", schemas=[SchemaMeta(name="s")],
        tables=tables, columns=columns, relationships=rels,
    )


def overlaps(table, default=None):
    """Fake overlap_fn from {(source_uid, target_uid): (n, hit)}."""

    def fn(source, target):
        found = table.get((source, target), default)
        if found is None:
            raise AssertionError(f"unexpected overlap query {source} -> {target}")
        return OverlapCounts(*found)

    return fn


def pairs(meta):
    return {(p.from_column, p.to_column): p.name_match for p in find_candidate_pairs(meta)}


# ---------------------------------------------------------------- type_family


@pytest.mark.parametrize(
    "data_type, family",
    [
        ("integer", "int"), ("bigint", "int"), ("smallint", "int"),
        ("numeric(10,2)", "numeric"), ("double precision", "numeric"),
        ("character varying(50)", "text"), ("text", "text"), ("character(3)", "text"),
        ("date", "time"), ("timestamp without time zone", "time"), ("timestamp with time zone", "time"),
        ("uuid", "uuid"), ("boolean", "boolean"),
        ("integer[]", "array:int"),
    ],
)
def test_type_family(data_type, family):
    assert type_family(data_type) == family


def test_integer_and_text_are_not_compatible():
    assert type_family("integer") != type_family("text")


# ---------------------------------------------------------------- name_signal


@pytest.mark.parametrize(
    "from_name, to_name, to_table, expected",
    [
        ("customerid", "customerid", "customer", "exact"),
        ("CustomerID", "customer_id", "customer", "exact"),  # case and underscores ignored
        ("billtoaddressid", "addressid", "address", "suffix"),
        ("buyer_customer_id", "customer_id", "customer", "suffix"),
        ("salespersonid", "businessentityid", "salesperson", "table_name"),
        ("customer_id", "id", "customer", "table_name"),  # id can only be reached via the table name
        ("customerid", "id", "customer", "table_name"),
        # --- must NOT match ---
        ("productcategoryid", "productid", "product", None),  # loose substring rule used to match this
        ("addresstypeid", "addressid", "address", None),
        ("id", "id", "widget", None),  # "id" == "id" says nothing
        ("thing_id", "id", "widget", None),
        ("fooxyz", "xyz", "t", None),  # suffix target shorter than 4 characters
        ("fooxyzw", "xyzw", "t", "suffix"),
        ("aid", "id", "a", None),  # table name too short to trust
        ("", "customerid", "customer", None),
        ("customerid", "", "customer", None),
        ("orderdate", "customerid", "customer", None),
    ],
)
def test_name_signal(from_name, to_name, to_table, expected):
    assert name_signal(from_name, to_name, to_table) == expected


def test_exact_beats_table_name_when_both_apply():
    assert name_signal("productid", "productid", "product") == "exact"


# ------------------------------------------------------ find_candidate_pairs


def two_tables(**kw):
    return meta_of(
        {
            "customer": (100, [("customerid", "integer", True)]),
            "orders": (500, [("orderid", "integer", True), ("customerid", "integer", False)]),
        },
        **kw,
    )


def test_basic_candidate_has_signal_and_compatible_type():
    found = find_candidate_pairs(two_tables())
    assert [(p.from_column, p.to_column, p.name_match, p.type_compatible) for p in found] == [
        (uid("orders", "customerid"), uid("customer", "customerid"), "exact", True)
    ]


def test_source_with_a_declared_relationship_is_skipped():
    declared = [(uid("orders", "customerid"), uid("customer", "customerid"))]
    assert find_candidate_pairs(two_tables(declared=declared)) == []


def test_source_with_a_composite_declared_key_is_skipped():
    # Each pair of a composite FK is a relationship, so both columns are taken.
    meta = meta_of(
        {
            "region": (10, [("countryid", "integer", False), ("regionid", "integer", False), ("rid", "integer", True)]),
            "store": (10, [("countryid", "integer", False), ("regionid", "integer", False)]),
        },
        declared=[
            (uid("store", "countryid"), uid("region", "countryid")),
            (uid("store", "regionid"), uid("region", "regionid")),
        ],
    )
    assert find_candidate_pairs(meta) == []


def test_generic_target_names_are_never_targets():
    meta = meta_of(
        {
            "a": (10, [("aid", "integer", True), ("rowguid", "uuid", True), ("modifieddate", "timestamp without time zone", True)]),
            "b": (10, [("bid", "integer", True), ("rowguid", "uuid", False), ("modifieddate", "timestamp without time zone", False)]),
        }
    )
    assert pairs(meta) == {}


def test_only_unique_columns_are_targets():
    meta = meta_of(
        {
            "customer": (100, [("customerid", "integer", False)]),  # not unique
            "orders": (500, [("customerid", "integer", False)]),
        }
    )
    assert pairs(meta) == {}


def test_incompatible_types_are_not_paired():
    meta = meta_of(
        {
            "customer": (100, [("customerid", "integer", True)]),
            "orders": (500, [("customerid", "text", False)]),
        }
    )
    assert pairs(meta) == {}


def test_columns_of_the_same_table_are_not_paired():
    meta = meta_of({"t": (10, [("employeeid", "integer", True), ("managerid", "integer", False), ("employee_id", "integer", False)])})
    assert pairs(meta) == {}


def test_id_columns_only_match_through_the_table_name():
    meta = meta_of(
        {
            "widget": (10, [("id", "integer", True)]),
            "gadget": (10, [("id", "integer", True)]),
            "part": (50, [("partid", "integer", True), ("widget_id", "integer", False)]),
        }
    )
    assert pairs(meta) == {(uid("part", "widget_id"), uid("widget", "id")): "table_name"}


# ---- direction rule: unique -> unique only goes from the smaller table to the larger


def unique_pair(rows_a, rows_b):
    return meta_of(
        {
            "parent": (rows_a, [("keyid", "integer", True)]),
            "child": (rows_b, [("keyid", "integer", True)]),
        }
    )


def test_unique_to_unique_points_from_smaller_to_larger():
    assert set(pairs(unique_pair(100, 10))) == {(uid("child", "keyid"), uid("parent", "keyid"))}
    assert set(pairs(unique_pair(10, 100))) == {(uid("parent", "keyid"), uid("child", "keyid"))}


def test_equal_sized_unique_tables_get_exactly_one_direction():
    found = pairs(unique_pair(50, 50))
    assert len(found) == 1
    ((source, target),) = found
    assert source < target  # the lower UID is the source: deterministic, never both ways


@pytest.mark.parametrize("source_name", ["buyer_keyid", "parentkeyid"])
def test_unique_source_needs_an_exact_name(source_name):
    # Two keys linked by a loose name (suffix / table-name) are sequential-id coincidence.
    meta = meta_of(
        {
            "big": (100, [("keyid", "integer", True)]),
            "small": (10, [(source_name, "integer", True)]),
        }
    )
    assert pairs(meta) == {}  # "keyid" is a suffix of both, but neither is an exact match


def test_unique_source_with_an_exact_name_is_allowed():
    assert len(pairs(unique_pair(100, 10))) == 1


def test_non_unique_source_may_use_a_loose_name():
    meta = meta_of(
        {
            "customer": (100, [("customerid", "integer", True)]),
            "review": (500, [("reviewid", "integer", True), ("buyer_customerid", "integer", False)]),
        }
    )
    assert pairs(meta) == {(uid("review", "buyer_customerid"), uid("customer", "customerid")): "suffix"}


def test_unknown_row_counts_do_not_block_a_pair():
    assert len(pairs(unique_pair(None, 10))) == 2


def test_non_unique_source_ignores_the_direction_rule():
    # A normal foreign key may point at a smaller table than itself.
    meta = meta_of(
        {
            "customer": (5, [("customerid", "integer", True)]),
            "orders": (500, [("orderid", "integer", True), ("customerid", "integer", False)]),
        }
    )
    assert (uid("orders", "customerid"), uid("customer", "customerid")) in pairs(meta)


def test_candidates_come_back_sorted():
    meta = meta_of(
        {
            "customer": (100, [("customerid", "integer", True)]),
            "b_orders": (500, [("customerid", "integer", False)]),
            "a_orders": (500, [("customerid", "integer", False)]),
        }
    )
    got = [p.from_column for p in find_candidate_pairs(meta)]
    assert got == sorted(got) and len(got) == 2


# ---------------------------------------------------------------- confidence


@pytest.mark.parametrize(
    "name_match, overlap, expected",
    [
        ("exact", 1.0, 0.95),  # capped: never as cheap as a declared join
        ("exact", 0.9, 0.94),
        ("suffix", 0.95, 0.91),
        ("table_name", 0.9, 0.82),
    ],
)
def test_confidence(name_match, overlap, expected):
    assert confidence(name_match, overlap) == expected


def test_confidence_never_reaches_one():
    assert max(confidence(n, 1.0) for n in ("exact", "suffix", "table_name")) < 1.0


# ---------------------------------------------------------- infer_relationships


def test_overlap_gate_is_inclusive_at_the_threshold():
    meta = two_tables()
    key = (uid("orders", "customerid"), uid("customer", "customerid"))
    assert len(infer_relationships(DB, meta, overlap_fn=overlaps({key: (100, 90)}))) == 1
    assert infer_relationships(DB, meta, overlap_fn=overlaps({key: (100, 89)})) == []


def test_empty_source_column_yields_nothing():
    key = (uid("orders", "customerid"), uid("customer", "customerid"))
    assert infer_relationships(DB, two_tables(), overlap_fn=overlaps({key: (0, 0)})) == []


def test_result_fields():
    key = (uid("orders", "customerid"), uid("customer", "customerid"))
    (rel,) = infer_relationships(DB, two_tables(), overlap_fn=overlaps({key: (100, 97)}))
    assert (rel.from_column, rel.to_column) == key
    assert rel.source == "inferred" and rel.constraint_name is None
    assert rel.cardinality == "N:1"
    assert rel.evidence.name_match == "exact" and rel.evidence.type_compatible is True
    assert rel.evidence.value_overlap == 0.97
    assert rel.confidence == confidence("exact", 0.97)


def test_unique_source_gives_a_one_to_one_relationship():
    meta = unique_pair(100, 10)
    key = (uid("child", "keyid"), uid("parent", "keyid"))
    (rel,) = infer_relationships(DB, meta, overlap_fn=overlaps({key: (10, 10)}))
    assert rel.cardinality == "1:1"


def parents(employee_rows=10, person_rows=100, extra_declared=()):
    return meta_of(
        {
            "employee": (employee_rows, [("businessentityid", "integer", True)]),
            "person": (person_rows, [("businessentityid", "integer", True)]),
            "pay": (500, [("payid", "integer", True), ("businessentityid", "integer", False)]),
        },
        declared=extra_declared,
    )


def test_tightest_parent_wins():
    meta = parents()
    src = uid("pay", "businessentityid")
    fn = overlaps({(src, uid("employee", "businessentityid")): (10, 10),
                   (src, uid("person", "businessentityid")): (10, 10)},
                  default=(0, 0))
    rels = [r for r in infer_relationships(DB, meta, overlap_fn=fn) if r.from_column == src]
    assert [r.to_column for r in rels] == [uid("employee", "businessentityid")]


def test_exact_name_beats_a_tighter_table_name_match():
    meta = meta_of(
        {
            "salesperson": (5, [("businessentityid", "integer", True)]),   # tight, but only via table name
            "big": (1000, [("salespersonid", "integer", True)]),           # looser, exact name
            "store": (50, [("storeid", "integer", True), ("salespersonid", "integer", False)]),
        }
    )
    src = uid("store", "salespersonid")
    fn = overlaps({(src, uid("salesperson", "businessentityid")): (5, 5),
                   (src, uid("big", "salespersonid")): (5, 5)}, default=(0, 0))
    (rel,) = [r for r in infer_relationships(DB, meta, overlap_fn=fn) if r.from_column == src]
    assert rel.to_column == uid("big", "salespersonid")


def test_most_referenced_table_breaks_a_tie():
    # Identical key sets (like person and password in AdventureWorks): the table
    # other columns already point at is the more likely parent.
    meta = meta_of(
        {
            "person": (20, [("businessentityid", "integer", True)]),
            "password": (20, [("businessentityid", "integer", True)]),
            "phone": (90, [("phoneid", "integer", True), ("businessentityid", "integer", False)]),
            "email": (90, [("emailid", "integer", True), ("businessentityid", "integer", False)]),
        },
        declared=[(uid("email", "businessentityid"), uid("person", "businessentityid"))],
    )
    src = uid("phone", "businessentityid")
    fn = overlaps({(src, uid("person", "businessentityid")): (20, 20),
                   (src, uid("password", "businessentityid")): (20, 20)}, default=(0, 0))
    (rel,) = [r for r in infer_relationships(DB, meta, overlap_fn=fn) if r.from_column == src]
    assert rel.to_column == uid("person", "businessentityid")


def test_a_complete_tie_goes_to_the_lowest_uid():
    meta = meta_of(
        {
            "b_target": (20, [("businessentityid", "integer", True)]),
            "a_target": (20, [("businessentityid", "integer", True)]),
            "src": (90, [("srcid", "integer", True), ("businessentityid", "integer", False)]),
        }
    )
    s = uid("src", "businessentityid")
    fn = overlaps({(s, uid("a_target", "businessentityid")): (20, 20),
                   (s, uid("b_target", "businessentityid")): (20, 20)}, default=(0, 0))
    (rel,) = [r for r in infer_relationships(DB, meta, overlap_fn=fn) if r.from_column == s]
    assert rel.to_column == uid("a_target", "businessentityid")


def test_a_stale_small_row_count_cannot_break_the_ranking():
    # Metadata says the parent has 3 rows but 10 distinct values matched.
    meta = two_tables()
    meta.tables[0] = meta.tables[0].model_copy(update={"row_count": 3})  # customer
    key = (uid("orders", "customerid"), uid("customer", "customerid"))
    assert len(infer_relationships(DB, meta, overlap_fn=overlaps({key: (10, 10)}))) == 1


def test_min_confidence_filters_the_result():
    key = (uid("orders", "customerid"), uid("customer", "customerid"))
    fn = overlaps({key: (100, 100)})
    assert len(infer_relationships(DB, two_tables(), min_confidence=0.95, overlap_fn=fn)) == 1
    assert infer_relationships(DB, two_tables(), min_confidence=0.96, overlap_fn=fn) == []


def test_at_most_one_target_per_source_column():
    meta = parents()
    fn = overlaps({}, default=(10, 10))
    sources = [r.from_column for r in infer_relationships(DB, meta, overlap_fn=fn)]
    assert len(sources) == len(set(sources))


def test_running_twice_adds_nothing():
    meta = two_tables()
    key = (uid("orders", "customerid"), uid("customer", "customerid"))
    fn = overlaps({key: (100, 100)})
    first = infer_relationships(DB, meta, overlap_fn=fn)
    again = meta.model_copy(update={"relationships": meta.relationships + first})
    assert infer_relationships(DB, again, overlap_fn=fn) == []


# ------------------------------------------------------------- with_inferred


def test_with_inferred_keeps_declared_and_recomputes_inferred():
    declared = [(uid("pay", "payid"), uid("person", "businessentityid"))]
    meta = parents(extra_declared=declared)
    src = uid("pay", "businessentityid")
    fn = overlaps({(src, uid("employee", "businessentityid")): (10, 10),
                   (src, uid("person", "businessentityid")): (10, 10)}, default=(0, 0))

    once = with_inferred(DB, meta, overlap_fn=fn)
    twice = with_inferred(DB, once, overlap_fn=fn)  # old inferred edges are dropped first
    assert once == twice
    assert [r for r in once.relationships if r.source == "declared"] == meta.relationships
    assert {r.from_column for r in once.relationships if r.source == "inferred"} >= {src}
    assert once.check_integrity() == []
    keys = [(r.from_column, r.to_column, r.constraint_name or "") for r in once.relationships]
    assert keys == sorted(keys)


def test_with_inferred_does_not_change_its_input():
    meta = two_tables()
    before = meta.model_dump()
    key = (uid("orders", "customerid"), uid("customer", "customerid"))
    with_inferred(DB, meta, overlap_fn=overlaps({key: (100, 100)}))
    assert meta.model_dump() == before
