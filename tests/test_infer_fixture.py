"""Relationship inference end to end on tests/fixtures/infer_fixture.sql.

Integration test: needs SG_TEST_PG_DSN (see conftest.py). Every case in the
fixture plants one behaviour; the golden sets below spell out which edges
inference must produce and which it must refuse.
"""

import psycopg
import pytest
from psycopg.rows import dict_row

from schemagraph.db import postgres as db
from schemagraph.extraction import extract as ex
from schemagraph.extraction import infer
from schemagraph.models import column_uid

pytestmark = pytest.mark.integration

DB = "fx"


def uid(table, column):
    return column_uid(DB, "shop", table, column)


def edge(src, dst):
    """('orders.customerid', 'customer.customer_id') -> pair of UIDs."""
    return (uid(*src.split(".")), uid(*dst.split(".")))


@pytest.fixture(scope="module")
def meta(infer_dsn):
    def run_admin_query(db_id, sql, params=None):
        with psycopg.connect(infer_dsn, row_factory=dict_row) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    mp = pytest.MonkeyPatch()
    mp.setattr(db, "run_admin_query", run_admin_query)
    try:
        yield ex.extract_metadata(DB, sample_n=0)
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def inferred(meta, infer_dsn):
    mp = pytest.MonkeyPatch()
    mp.setattr(
        db,
        "run_admin_query",
        lambda db_id, sql, params=None: _query(infer_dsn, sql, params),
    )
    try:
        return infer.infer_relationships(DB, meta)
    finally:
        mp.undo()


def _query(dsn, sql, params):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


EXPECTED = {
    # exact name
    edge("orders.customerid", "customer.customer_id"),
    edge("orders.productid", "product.productid"),
    # suffix name
    edge("review.buyer_customer_id", "customer.customer_id"),
    # table-name pattern
    edge("store.salespersonid", "salesperson.businessentityid"),
    edge("part.widget_id", "widget.id"),
    # tightest parent wins
    edge("pay.businessentityid", "employee.businessentityid"),
    # shared keys (1:1), smaller table -> larger table
    edge("employee.businessentityid", "person.businessentityid"),
    edge("salesperson.businessentityid", "employee.businessentityid"),
    edge("customer.customer_id", "customer_extra.customerid"),
}


def test_inferred_set_matches_golden(inferred):
    assert {(r.from_column, r.to_column) for r in inferred} == EXPECTED


def test_every_inferred_relationship_is_marked_inferred(inferred):
    assert inferred and all(r.source == "inferred" for r in inferred)
    assert all(r.evidence is not None for r in inferred)


def test_confidence_is_bounded_and_ordered_by_name_signal(inferred):
    by_pair = {(r.from_column, r.to_column): r for r in inferred}
    assert all(0.5 <= r.confidence <= infer.MAX_INFERRED_CONFIDENCE for r in inferred)
    exact = by_pair[edge("orders.customerid", "customer.customer_id")]
    table_name = by_pair[edge("part.widget_id", "widget.id")]
    assert exact.confidence > table_name.confidence


def test_name_match_labels(inferred):
    labels = {(r.from_column, r.to_column): r.evidence.name_match for r in inferred}
    assert labels[edge("orders.customerid", "customer.customer_id")] == "exact"
    assert labels[edge("review.buyer_customer_id", "customer.customer_id")] == "suffix"
    assert labels[edge("store.salespersonid", "salesperson.businessentityid")] == "table_name"


def test_cardinality_follows_source_uniqueness(inferred):
    card = {(r.from_column, r.to_column): r.cardinality for r in inferred}
    assert card[edge("orders.customerid", "customer.customer_id")] == "N:1"
    assert card[edge("employee.businessentityid", "person.businessentityid")] == "1:1"


@pytest.mark.parametrize(
    "src,dst,why",
    [
        ("productreview.productreviewid", "product.productid", "name does not match"),
        ("productreview.productreviewid", "review.reviewid", "loose name between two keys"),
        ("lead.customerid", "customer.customer_id", "values mostly absent from the parent"),
        ("draft.customerid", "customer.customer_id", "empty column"),
        ("invoice.customerid", "customer.customer_id", "already declared"),
        ("b.rowguid", "a.rowguid", "generic name"),
        ("gadget.id", "widget.id", "'id' is too generic to match by name"),
        ("widget.id", "gadget.id", "'id' is too generic to match by name"),
        ("person.businessentityid", "employee.businessentityid", "never both directions"),
        ("customer_extra.customerid", "customer.customer_id", "never both directions"),
    ],
)
def test_refused(inferred, src, dst, why):
    assert edge(src, dst) not in {(r.from_column, r.to_column) for r in inferred}, why


def test_one_target_per_source(inferred):
    sources = [r.from_column for r in inferred]
    assert len(sources) == len(set(sources))


def test_declared_relationship_is_untouched(meta):
    declared = [r for r in meta.relationships if r.source == "declared"]
    assert [(r.from_column, r.to_column) for r in declared] == [
        edge("invoice.customerid", "customer.customer_id")
    ]


def test_with_inferred_replaces_old_inferred_and_keeps_integrity(meta, infer_dsn):
    mp = pytest.MonkeyPatch()
    mp.setattr(db, "run_admin_query", lambda db_id, sql, params=None: _query(infer_dsn, sql, params))
    try:
        once = infer.with_inferred(DB, meta)
        twice = infer.with_inferred(DB, once)  # idempotent: old inferred edges are dropped first
    finally:
        mp.undo()
    key = lambda m: sorted((r.from_column, r.to_column, r.source) for r in m.relationships)  # noqa: E731
    assert key(once) == key(twice)
    assert len([r for r in once.relationships if r.source == "declared"]) == 1
    assert len([r for r in once.relationships if r.source == "inferred"]) == len(EXPECTED)
