"""Unit tests for the pure logic in extraction/extract.py. No database needed."""

import logging

import psycopg
import pytest

from schemagraph.db import postgres as db
from schemagraph.extraction import extract as ex
from schemagraph.extraction.extract import (
    build_columns,
    build_relationships,
    build_tables,
    first_distinct,
    group_primary_keys,
    group_unique_sets,
    is_one_to_one,
    single_unique_columns,
    skip_sample_type,
)
from schemagraph.models import DatabaseMetadata, SchemaMeta

DB = "d"


def uq(schema, table, index, column):
    return {"schema_name": schema, "table_name": table, "index_name": index, "column_name": column}


# ------------------------------------------------------------------ grouping


def test_primary_key_columns_follow_key_position_not_row_order():
    rows = [
        {"schema_name": "s", "table_name": "t", "column_name": "b", "key_position": 2},
        {"schema_name": "s", "table_name": "t", "column_name": "a", "key_position": 1},
        {"schema_name": "s", "table_name": "u", "column_name": "id", "key_position": 1},
    ]
    assert group_primary_keys(DB, rows) == {"d:s.t": ["a", "b"], "d:s.u": ["id"]}


def test_unique_groups_and_single_columns():
    rows = [
        uq("s", "t", "t_pkey", "a"),  # composite key (a, b)
        uq("s", "t", "t_pkey", "b"),
        uq("s", "t", "t_email_key", "email"),  # single-column unique
    ]
    assert group_unique_sets(DB, rows) == {
        "d:s.t": [frozenset({"email"}), frozenset({"a", "b"})]
    }
    # Only the one-column index makes a column unique on its own.
    assert single_unique_columns(DB, rows) == {"d:s.t.email"}


# --------------------------------------------------------------- cardinality


@pytest.mark.parametrize(
    "fk_columns, unique_sets, expected",
    [
        (["personid"], [frozenset({"personid"})], True),  # unique FK column
        (["customerid"], [frozenset({"salesorderid"})], False),  # plain N:1
        (["a", "b"], [frozenset({"a", "b"})], True),  # composite FK that is the key
        (["a", "b"], [frozenset({"a"})], True),  # contains a smaller unique set
        (["a"], [frozenset({"a", "b"})], False),  # only part of a composite key
        (["a"], [], False),
    ],
)
def test_is_one_to_one(fk_columns, unique_sets, expected):
    assert is_one_to_one(fk_columns, unique_sets) is expected


# ------------------------------------------------------------ sample helpers


def test_first_distinct_keeps_order_and_skips_blanks():
    values = ["b", "a", None, "", "  ", "b", "c", "d"]
    assert first_distinct(values, 3) == ["b", "a", "c"]


def test_first_distinct_stops_early():
    def endless():
        yield from ("x", "y", "z")
        raise AssertionError("kept reading after n values were found")

    assert first_distinct(endless(), 3) == ["x", "y", "z"]


@pytest.mark.parametrize(
    "data_type, skipped",
    [
        ("bytea", True),
        ("xml", True),
        ("XML", True),
        ("bytea[]", True),
        ("tsvector", True),
        ("text", False),
        ("character varying(50)", False),
        ("integer", False),
        ("jsonb", False),
    ],
)
def test_skip_sample_type(data_type, skipped):
    assert skip_sample_type(data_type) is skipped


def test_sample_values_skips_binary_without_querying(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not query for a bytea column")

    monkeypatch.setattr(db, "run_admin_query", boom)
    assert ex._sample_values(DB, "s", "t", "c", "bytea", 5) == []
    assert ex._sample_values(DB, "s", "t", "c", "text", 0) == []  # n == 0


def test_sample_values_dedupes_and_drops_nulls(monkeypatch):
    rows = [{"v": "a"}, {"v": "a"}, {"v": None}, {"v": "b"}, {"v": "c"}]
    monkeypatch.setattr(db, "run_admin_query", lambda *a, **k: rows)
    assert ex._sample_values(DB, "s", "t", "c", "text", 2) == ["a", "b"]


def test_sample_values_survives_a_database_error(monkeypatch, caplog):
    def fail(*a, **k):
        raise psycopg.errors.InsufficientPrivilege("permission denied for table t")

    monkeypatch.setattr(db, "run_admin_query", fail)
    with caplog.at_level(logging.WARNING):
        assert ex._sample_values(DB, "s", "t", "c", "text", 5) == []
    assert "no sample values for s.t.c" in caplog.text


# ------------------------------------------------------------------- tables


def table_row(name, estimate, description=None):
    return {"schema_name": "s", "table_name": name, "description": description, "row_estimate": estimate}


def test_row_count_uses_estimate_or_falls_back_to_exact_count():
    calls = []

    def count(schema, table):
        calls.append(table)
        return 7

    rows = [table_row("estimated", 123), table_row("never_analyzed", None), table_row("zero", 0)]
    tables = {t.name: t for t in build_tables(DB, rows, {}, count)}

    assert tables["estimated"].row_count == 123
    assert tables["never_analyzed"].row_count == 7
    assert tables["zero"].row_count == 7  # an estimate of 0 is not trusted
    assert calls == ["never_analyzed", "zero"]  # exact count only where needed


def test_table_description_and_primary_key():
    rows = [table_row("a", 1, "Hello."), table_row("b", 1, ""), table_row("c", 1, None)]
    tables = {t.name: t for t in build_tables(DB, rows, {"d:s.a": ["x", "y"]}, lambda *a: 0)}
    assert (tables["a"].description, tables["a"].description_source) == ("Hello.", "catalog")
    # An empty comment means "no description", not a description of "".
    assert (tables["b"].description, tables["b"].description_source) == (None, None)
    assert (tables["c"].description, tables["c"].description_source) == (None, None)
    assert tables["a"].primary_key == ["x", "y"]
    assert tables["a"].uid == "d:s.a"


# ------------------------------------------------------------------ columns


def test_column_flags_and_uids():
    row = {
        "schema_name": "s", "table_name": "t", "column_name": "id", "data_type": "integer",
        "ordinal_position": 1, "is_nullable": False, "description": "Key.",
    }
    other = {**row, "column_name": "name", "ordinal_position": 2, "is_nullable": True, "description": None}
    cols = {
        c.name: c
        for c in build_columns(DB, [row, other], {"d:s.t": ["id"]}, {"d:s.t.id"})
    }
    assert cols["id"].uid == "d:s.t.id" and cols["id"].table_uid == "d:s.t"
    assert cols["id"].is_primary_key and cols["id"].is_unique and not cols["id"].is_nullable
    assert (cols["id"].description, cols["id"].description_source) == ("Key.", "catalog")
    assert not cols["name"].is_primary_key and not cols["name"].is_unique
    assert cols["name"].description_source is None
    assert cols["name"].sample_values == []


# ----------------------------------------------------------- relationships


def fk(constraint, from_table, from_col, to_table, to_col, pos=1):
    return {
        "constraint_name": constraint,
        "from_schema": "s", "from_table": from_table, "from_column": from_col,
        "to_schema": "s", "to_table": to_table, "to_column": to_col,
        "key_position": pos,
    }


def test_composite_fk_becomes_one_relationship_per_pair_sharing_a_constraint():
    rows = [
        fk("fk_store_region", "store", "regionid", "region", "regionid", pos=2),
        fk("fk_store_region", "store", "countryid", "region", "countryid", pos=1),
    ]
    rels = build_relationships(DB, rows, {})
    assert {(r.from_column, r.to_column) for r in rels} == {
        ("d:s.store.countryid", "d:s.region.countryid"),
        ("d:s.store.regionid", "d:s.region.regionid"),
    }
    assert {r.constraint_name for r in rels} == {"fk_store_region"}
    assert {r.cardinality for r in rels} == {"N:1"}
    assert all(r.source == "declared" and r.confidence == 1.0 and r.evidence is None for r in rels)


def test_cardinality_is_decided_per_constraint_on_all_its_columns():
    rows = [
        fk("fk_detail", "detail", "a", "region", "a", 1),
        fk("fk_detail", "detail", "b", "region", "b", 2),
        fk("fk_other", "plain", "x", "region", "a", 1),
    ]
    unique = {"d:s.detail": [frozenset({"a", "b"})], "d:s.plain": [frozenset({"id"})]}
    by_constraint = {r.constraint_name: r.cardinality for r in build_relationships(DB, rows, unique)}
    assert by_constraint == {"fk_detail": "1:1", "fk_other": "N:1"}


def test_same_constraint_name_on_different_tables_stays_separate():
    # PostgreSQL only needs constraint names to be unique per table.
    rows = [fk("fk_1", "t1", "a", "r", "id"), fk("fk_1", "t2", "a", "r", "id")]
    assert len(build_relationships(DB, rows, {})) == 2


def test_relationships_are_sorted_for_stable_output():
    rows = [fk("z", "t2", "a", "r", "id"), fk("a", "t1", "a", "r", "id")]
    rels = build_relationships(DB, rows, {})
    assert [r.from_column for r in rels] == sorted(r.from_column for r in rels)


# --------------------------------------------------------------- save / load


def small_metadata() -> DatabaseMetadata:
    tables = build_tables(DB, [table_row("t", 5, "A table.")], {"d:s.t": ["id"]}, lambda *a: 0)
    cols = build_columns(
        DB,
        [{"schema_name": "s", "table_name": "t", "column_name": "id", "data_type": "integer",
          "ordinal_position": 1, "is_nullable": False, "description": None}],
        {"d:s.t": ["id"]},
        {"d:s.t.id"},
    )
    return DatabaseMetadata(
        db_id=DB,
        extracted_at="2026-10-25T12:00:00Z",
        schemas=[SchemaMeta(name="s")],
        tables=tables,
        columns=cols,
        relationships=[],
    )


def test_save_uses_contract_field_names_and_round_trips(tmp_path):
    meta = small_metadata()
    path = tmp_path / "nested" / "d.json"  # parent folders are created
    ex.save_metadata(meta, path)

    text = path.read_text()
    assert '"schema": "s"' in text  # the contract's name, not the Python attribute
    assert "schema_" not in text
    assert ex.load_metadata(path) == meta
    assert list(path.parent.glob("*.tmp")) == []  # atomic write left nothing behind


def test_failed_save_leaves_existing_file_untouched(tmp_path, monkeypatch):
    meta = small_metadata()
    path = tmp_path / "d.json"
    ex.save_metadata(meta, path)
    before = path.read_text()

    monkeypatch.setattr(ex.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        ex.save_metadata(meta, path)
    assert path.read_text() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_default_metadata_path():
    assert str(ex.default_metadata_path("adventureworks")) == "data/metadata/adventureworks.json"
