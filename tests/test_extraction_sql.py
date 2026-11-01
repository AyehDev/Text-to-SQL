"""Runs each extraction/sql/*.sql file against tests/fixtures/extraction_fixture.sql.

Integration tests: need SG_TEST_PG_DSN (see conftest.py).
"""

from collections import defaultdict
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

SQL_DIR = Path(__file__).parent.parent / "schemagraph" / "extraction" / "sql"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def conn(fixture_dsn):
    with psycopg.connect(fixture_dsn, row_factory=dict_row, autocommit=True) as c:
        yield c


def run(conn, name: str) -> list[dict]:
    # Same call shape as run_admin_query: no params, so "%" is never special.
    return conn.execute((SQL_DIR / name).read_text()).fetchall()


# --------------------------------------------------------------------------- tables


def test_tables_lists_base_tables_only(conn):
    got = {(r["schema_name"], r["table_name"]) for r in run(conn, "tables.sql")}
    assert got == {
        ("person", "person"),
        ("person", "passport"),
        ("sales", "customer"),
        ("sales", "salesorderheader"),
        ("sales", "region"),
        ("sales", "store"),
        ("sales", "region_detail"),
        ("sales", "promo"),
        ("sales", "sku"),
        ("sales", "dropped"),
        ("sales", "events"),  # the partitioned parent stands for its partitions
        ("sales", "event_note"),
        ("sales", "blobs"),
        ("sales", "analyzed"),
        ("Mixed", "Order Line"),  # exact case and the space survive
    }
    assert ("sales", "v_customer") not in got  # view
    assert ("sales", "events_2024") not in got  # partition


def test_tables_have_no_system_schemas(conn):
    schemas = {r["schema_name"] for r in run(conn, "tables.sql")}
    assert not {s for s in schemas if s.startswith("pg_") or s == "information_schema"}


def test_table_descriptions(conn):
    rows = {(r["schema_name"], r["table_name"]): r for r in run(conn, "tables.sql")}
    assert rows[("person", "person")]["description"] == "Human beings."
    assert rows[("sales", "customer")]["description"] is None


def test_row_estimate_null_until_analyzed(conn):
    rows = {(r["schema_name"], r["table_name"]): r for r in run(conn, "tables.sql")}
    assert rows[("sales", "analyzed")]["row_estimate"] == 123
    assert rows[("sales", "customer")]["row_estimate"] is None  # never analyzed


# ------------------------------------------------------------------------- columns


def test_columns_types_nullability_and_descriptions(conn):
    cols = {
        (r["schema_name"], r["table_name"], r["column_name"]): r for r in run(conn, "columns.sql")
    }
    first = cols[("person", "person", "firstname")]
    assert first["data_type"] == "character varying(50)"
    assert first["is_nullable"] is False
    assert first["description"] == "First name."
    assert cols[("person", "person", "email")]["is_nullable"] is True
    assert cols[("sales", "salesorderheader", "totaldue")]["data_type"] == "numeric(10,2)"
    assert cols[("Mixed", "Order Line", "LineId")]["data_type"] == "integer"


def test_ordinal_position_is_dense_after_dropped_column(conn):
    rows = [r for r in run(conn, "columns.sql") if r["table_name"] == "dropped"]
    assert [(r["column_name"], r["ordinal_position"]) for r in rows] == [("a", 1), ("c", 2)]


def test_columns_only_for_listed_tables(conn):
    table_keys = {(r["schema_name"], r["table_name"]) for r in run(conn, "tables.sql")}
    col_keys = {(r["schema_name"], r["table_name"]) for r in run(conn, "columns.sql")}
    assert col_keys == table_keys


# ------------------------------------------------------------------------ primary keys


def test_primary_key_order_is_preserved(conn):
    pks = defaultdict(list)
    for r in run(conn, "primary_keys.sql"):
        pks[(r["schema_name"], r["table_name"])].append((r["key_position"], r["column_name"]))
    assert pks[("sales", "region")] == [(1, "countryid"), (2, "regionid")]
    assert pks[("sales", "events")] == [(1, "eventid"), (2, "ts")]
    assert pks[("person", "person")] == [(1, "businessentityid")]


def test_every_table_has_a_primary_key_here(conn):
    with_pk = {(r["schema_name"], r["table_name"]) for r in run(conn, "primary_keys.sql")}
    tables = {(r["schema_name"], r["table_name"]) for r in run(conn, "tables.sql")}
    assert with_pk == tables


# ------------------------------------------------------------------------ foreign keys


def _fk_groups(conn):
    groups = defaultdict(list)
    for r in run(conn, "foreign_keys.sql"):
        groups[r["constraint_name"]].append(r)
    return groups


def test_composite_fk_is_paired_column_by_column(conn):
    rows = _fk_groups(conn)["fk_store_region"]
    assert [(r["key_position"], r["from_column"], r["to_column"]) for r in rows] == [
        (1, "countryid", "countryid"),
        (2, "regionid", "regionid"),
    ]
    assert {(r["from_schema"], r["from_table"], r["to_schema"], r["to_table"]) for r in rows} == {
        ("sales", "store", "sales", "region")
    }


def test_single_column_fk_across_schemas(conn):
    rows = _fk_groups(conn)["customer_personid_fkey"]
    assert len(rows) == 1
    r = rows[0]
    assert (r["from_schema"], r["from_table"], r["from_column"]) == ("sales", "customer", "personid")
    assert (r["to_schema"], r["to_table"], r["to_column"]) == ("person", "person", "businessentityid")


def test_partitioned_table_fk_appears_once(conn):
    rows = [r for r in run(conn, "foreign_keys.sql") if r["from_table"].startswith("events")]
    assert len(rows) == 1  # not duplicated by the partition's cloned constraint
    assert rows[0]["from_table"] == "events"


def test_fk_to_partitioned_table_appears_once(conn):
    rows = _fk_groups(conn)["fk_note_event"]
    assert [(r["key_position"], r["to_table"], r["to_column"]) for r in rows] == [
        (1, "events", "eventid"),
        (2, "events", "ts"),
    ]
    # No clone constraints pointing at the partition itself.
    assert not [r for r in run(conn, "foreign_keys.sql") if r["to_table"] == "events_2024"]


def test_fk_endpoints_are_all_listed_tables(conn):
    tables = {(r["schema_name"], r["table_name"]) for r in run(conn, "tables.sql")}
    for r in run(conn, "foreign_keys.sql"):
        assert (r["from_schema"], r["from_table"]) in tables
        assert (r["to_schema"], r["to_table"]) in tables


# ------------------------------------------------------------------- unique constraints


def _unique_sets(conn):
    groups = defaultdict(list)
    for r in run(conn, "unique_constraints.sql"):
        groups[(r["schema_name"], r["table_name"], r["index_name"])].append(r["column_name"])
    return {k: tuple(v) for k, v in groups.items()}


def test_unique_constraint_on_fk_column_is_found(conn):
    sets = _unique_sets(conn)
    assert ("person", "passport", "passport_personid_key") in sets
    assert sets[("person", "passport", "passport_personid_key")] == ("personid",)


def test_composite_primary_key_is_one_group_not_two_unique_columns(conn):
    sets = _unique_sets(conn)
    assert sets[("sales", "region", "region_pkey")] == ("countryid", "regionid")


def test_partial_and_expression_indexes_are_ignored(conn):
    names = {k[2] for k in _unique_sets(conn)}
    assert "ux_promo_active_code" not in names  # partial
    assert "ux_person_email_lower" not in names  # expression


def test_mixed_column_and_expression_index_is_ignored(conn):
    sets = _unique_sets(conn)
    assert ("sales", "dropped", "ux_dropped_mixed") not in sets
    # Without the indexprs filter this would show up as ("c",): a false "c is unique".
    assert not [k for k, cols in sets.items() if k[:2] == ("sales", "dropped") and cols == ("c",)]


def test_include_columns_are_not_part_of_the_key(conn):
    assert _unique_sets(conn)[("sales", "sku", "ux_sku_code")] == ("code",)
