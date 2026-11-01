"""extract_metadata() end to end on the fixture database.

Integration test: needs SG_TEST_PG_DSN (see conftest.py). It swaps
run_admin_query for a function that connects to the scratch database
directly, so the real SQL files and the real assembly code are exercised
without needing the sg_admin role.
"""

import psycopg
import pytest
from psycopg.rows import dict_row

from schemagraph.db import postgres as db
from schemagraph.extraction import extract as ex

pytestmark = pytest.mark.integration

DB = "fx"


@pytest.fixture(scope="module")
def meta(fixture_dsn):
    def run_admin_query(db_id, sql, params=None):
        with psycopg.connect(fixture_dsn, row_factory=dict_row) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    mp = pytest.MonkeyPatch()
    mp.setattr(db, "run_admin_query", run_admin_query)
    try:
        yield ex.extract_metadata(DB)
    finally:
        mp.undo()


@pytest.fixture
def run_admin(fixture_dsn, monkeypatch):
    """Same patch, for tests that call the fetch_* functions directly."""

    def run_admin_query(db_id, sql, params=None):
        with psycopg.connect(fixture_dsn, row_factory=dict_row) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    monkeypatch.setattr(db, "run_admin_query", run_admin_query)


def table(meta, schema, name):
    return next(t for t in meta.tables if t.uid == f"{DB}:{schema}.{name}")


def col(meta, schema, tbl, name):
    return next(c for c in meta.columns if c.uid == f"{DB}:{schema}.{tbl}.{name}")


def rels(meta, constraint):
    return [r for r in meta.relationships if r.constraint_name == constraint]


# ----------------------------------------------------------------- whole object


def test_metadata_is_internally_consistent(meta):
    assert meta.check_integrity() == []
    assert meta.db_id == DB


def test_schemas_are_only_those_with_tables(meta):
    assert [s.name for s in meta.schemas] == ["Mixed", "person", "sales"]


def test_every_table_has_columns_and_every_column_a_table(meta):
    table_uids = {t.uid for t in meta.tables}
    assert {c.table_uid for c in meta.columns} == table_uids


def test_mixed_case_names_are_kept_exactly(meta):
    assert table(meta, "Mixed", "Order Line").uid == "fx:Mixed.Order Line"
    assert col(meta, "Mixed", "Order Line", "LineId").uid == "fx:Mixed.Order Line.LineId"


# ------------------------------------------------------- keys, uniqueness, flags


def test_primary_keys_in_key_order(meta):
    assert table(meta, "sales", "region").primary_key == ["countryid", "regionid"]
    assert table(meta, "sales", "events").primary_key == ["eventid", "ts"]


def test_primary_key_and_unique_flags(meta):
    assert col(meta, "sales", "region", "countryid").is_primary_key
    assert not col(meta, "sales", "region", "countryid").is_unique  # only unique as a pair
    assert col(meta, "person", "person", "businessentityid").is_unique
    assert col(meta, "person", "passport", "personid").is_unique  # UNIQUE constraint
    assert not col(meta, "person", "passport", "personid").is_primary_key
    assert col(meta, "sales", "sku", "code").is_unique  # INCLUDE column ignored
    assert not col(meta, "sales", "promo", "code").is_unique  # partial index only
    assert not col(meta, "sales", "dropped", "c").is_unique  # mixed expression index


# ----------------------------------------------------------------- relationships


def test_cardinality(meta):
    assert {r.cardinality for r in rels(meta, "fk_soh_customer")} == {"N:1"}
    assert {r.cardinality for r in rels(meta, "fk_passport_person")} == {"1:1"}
    assert {r.cardinality for r in rels(meta, "fk_store_region")} == {"N:1"}
    # Composite FK whose columns are the whole primary key: 1:1 as a set.
    assert {r.cardinality for r in rels(meta, "fk_detail_region")} == {"1:1"}


def test_composite_fk_pairs_columns_correctly(meta):
    pairs = {(r.from_column, r.to_column) for r in rels(meta, "fk_store_region")}
    assert pairs == {
        ("fx:sales.store.countryid", "fx:sales.region.countryid"),
        ("fx:sales.store.regionid", "fx:sales.region.regionid"),
    }


def test_partitioned_table_relationships_are_not_duplicated(meta):
    assert len([r for r in meta.relationships if "events_2024" in r.from_column + r.to_column]) == 0
    assert len(rels(meta, "fk_note_event")) == 2  # one pair per column, no clones


def test_all_relationships_are_declared(meta):
    assert all(r.source == "declared" and r.confidence == 1.0 for r in meta.relationships)


# ----------------------------------------------------------------- row counts


def test_row_counts(meta):
    assert table(meta, "sales", "analyzed").row_count == 123  # planner estimate
    assert table(meta, "sales", "promo").row_count == 3  # never analyzed: exact count
    assert table(meta, "person", "person").row_count == 3
    assert table(meta, "sales", "customer").row_count == 0  # empty table


def test_descriptions(meta):
    person = table(meta, "person", "person")
    assert (person.description, person.description_source) == ("Human beings.", "catalog")
    assert table(meta, "sales", "customer").description_source is None
    assert col(meta, "person", "person", "firstname").description == "First name."


# ----------------------------------------------------------------- sample values


def test_sample_values_are_distinct_and_skip_nulls(meta):
    assert set(col(meta, "person", "person", "firstname").sample_values) == {"Jon", "Eugene"}
    assert len(col(meta, "person", "person", "firstname").sample_values) == 2  # no repeat of "Jon"
    assert set(col(meta, "person", "person", "email").sample_values) == {
        "jon@example.com",
        "jon2@example.com",
    }


def test_numbers_become_strings(meta):
    assert set(col(meta, "sales", "analyzed", "id").sample_values) == {"1", "2", "3", "4", "5"}


def test_long_values_are_truncated(meta):
    values = col(meta, "sales", "region", "name").sample_values
    assert set(values) == {"North", "x" * ex.SAMPLE_MAX_CHARS}


def test_binary_xml_and_empty_columns_have_no_samples(meta):
    assert col(meta, "sales", "blobs", "data").sample_values == []  # bytea
    assert col(meta, "sales", "blobs", "doc").sample_values == []  # xml
    assert col(meta, "sales", "blobs", "label").sample_values == []  # only '' and NULL
    assert col(meta, "sales", "customer", "customerid").sample_values == []  # empty table


def test_sample_n_zero_skips_sampling(run_admin):
    plain = ex.extract_metadata(DB, sample_n=0)
    assert all(c.sample_values == [] for c in plain.columns)


def test_sample_count_is_capped(run_admin):
    meta = ex.extract_metadata(DB, sample_n=2)
    assert len(next(c for c in meta.columns if c.uid == "fx:sales.analyzed.id").sample_values) == 2


# ----------------------------------------------------------- single-item fetchers


def test_fetch_sample_values_resolves_the_uid_from_the_catalog(run_admin):
    assert set(ex.fetch_sample_values(DB, "fx:person.person.firstname")) == {"Jon", "Eugene"}
    assert ex.fetch_sample_values(DB, "fx:Mixed.Order Line.LineId") == []  # empty table, exact-case name


def test_fetch_sample_values_accepts_the_spec_keyword_names(run_admin):
    # SPEC 5.3: fetch_sample_values(db_id, column_uid, n=5)
    values = ex.fetch_sample_values(db_id=DB, column_uid="fx:sales.analyzed.id", n=3)
    assert len(values) == 3


def test_fetch_sample_values_unknown_column(run_admin):
    with pytest.raises(ValueError, match="No such column"):
        ex.fetch_sample_values(DB, "fx:person.person.nope")


def test_fetchers_agree_with_extract_metadata(run_admin, meta):
    assert ex.fetch_tables(DB) == meta.tables
    assert ex.fetch_foreign_keys(DB) == meta.relationships
    assert ex.fetch_primary_keys(DB)["fx:sales.region"] == ["countryid", "regionid"]
    assert "fx:person.passport.personid" in ex.fetch_unique_columns(DB)
    plain_cols = ex.fetch_columns(DB)
    assert [c.uid for c in plain_cols] == [c.uid for c in meta.columns]
    assert all(c.sample_values == [] for c in plain_cols)  # sampling is separate


# ---------------------------------------------------------------- save and load


def test_saved_file_loads_back_identical(meta, tmp_path):
    path = tmp_path / "fx.json"
    ex.save_metadata(meta, path)
    assert ex.load_metadata(path) == meta


def test_extraction_is_deterministic_apart_from_the_timestamp(run_admin, meta):
    again = ex.extract_metadata(DB)
    assert again.model_copy(update={"extracted_at": meta.extracted_at}) == meta
