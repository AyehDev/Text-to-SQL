"""The hide-and-recover harness, on a small hand-built schema and a fake overlap."""

import pytest

from schemagraph.eval import infer_recovery as rec
from schemagraph.extraction.infer import OverlapCounts
from tests.test_infer_unit import meta_of, overlaps, uid

SPEC = {
    "customer": (100, [("customerid", "integer", True)]),
    "product": (50, [("productid", "integer", True)]),
    "orders": (500, [("orderid", "integer", True), ("customerid", "integer", False),
                     ("productid", "integer", False)]),
    "pair": (10, [("pairid", "integer", True), ("a", "integer", False), ("b", "integer", False)]),
}
ORDERS_CUST = (uid("orders", "customerid"), uid("customer", "customerid"))
ORDERS_PROD = (uid("orders", "productid"), uid("product", "productid"))
# pair.a/pair.b have names no rule can reach: declared, but never findable
PAIR_A = (uid("pair", "a"), uid("customer", "customerid"))


def full_overlap():
    return overlaps({}, default=(10, 10))


def declared_meta(*declared):
    return meta_of(SPEC, declared=list(declared))


def test_single_column_declared_skips_composite_constraints():
    meta = declared_meta(ORDERS_CUST, ORDERS_PROD)
    # make the two relationships share one constraint -> composite, so excluded
    for r in meta.relationships:
        r.constraint_name = "fk_shared"
    assert rec.single_column_declared(meta) == []


def test_single_column_declared_lists_each_single_constraint():
    meta = declared_meta(ORDERS_CUST, ORDERS_PROD)
    assert rec.single_column_declared(meta) == sorted([ORDERS_CUST, ORDERS_PROD])


def test_single_column_declared_ignores_inferred():
    meta = declared_meta(ORDERS_CUST)
    meta.relationships[0].source = "inferred"
    assert rec.single_column_declared(meta) == []


def test_hide_is_seeded_and_does_not_mutate():
    meta = declared_meta(ORDERS_CUST, ORDERS_PROD)
    a, hidden_a = rec.hide(meta, 0.5, seed=3)
    b, hidden_b = rec.hide(meta, 0.5, seed=3)
    assert hidden_a == hidden_b and len(hidden_a) == 1
    assert len(a.relationships) == 1 == len(b.relationships)
    assert len(meta.relationships) == 2  # original untouched
    assert not hidden_a & {(r.from_column, r.to_column) for r in a.relationships}


def test_hide_fraction_extremes():
    meta = declared_meta(ORDERS_CUST, ORDERS_PROD)
    assert rec.hide(meta, 0.0, 0)[1] == set()
    kept, hidden = rec.hide(meta, 1.0, 0)
    assert hidden == {ORDERS_CUST, ORDERS_PROD} and kept.relationships == []


def test_cached_calls_underlying_once_per_pair():
    calls = []

    def fn(s, t):
        calls.append((s, t))
        return OverlapCounts(1, 1)

    wrapped = rec.cached(fn)
    assert wrapped("a", "b") == wrapped("a", "b") == OverlapCounts(1, 1)
    wrapped("b", "a")
    assert calls == [("a", "b"), ("b", "a")]


def test_run_recovery_recovers_hidden_fks():
    meta = declared_meta(ORDERS_CUST, ORDERS_PROD)
    (r,) = rec.run_recovery("d", meta, 1.0, [0], overlap_fn=full_overlap())
    assert r.hidden == r.recovered == r.findable == {ORDERS_CUST, ORDERS_PROD}
    assert r.recall == 1.0 and r.wrong_target == [] and r.extra == []


def test_run_recovery_reports_unfindable_and_missed():
    meta = declared_meta(ORDERS_CUST, PAIR_A)
    (r,) = rec.run_recovery("d", meta, 1.0, [0], overlap_fn=full_overlap())
    assert r.recovered == {ORDERS_CUST}
    assert r.findable == {ORDERS_CUST}  # pair.a has no name rule that reaches customerid
    assert r.recall == 0.5


def test_run_recovery_low_overlap_is_missed_not_wrong():
    meta = declared_meta(ORDERS_CUST)
    (r,) = rec.run_recovery("d", meta, 1.0, [0], overlap_fn=overlaps({}, default=(10, 2)))
    assert r.recovered == set() and r.wrong_target == [] and r.findable == {ORDERS_CUST}


def test_run_recovery_counts_wrong_target():
    # truth: orders.customerid -> customer.customerid; but an equally named alternative
    # exists and is the tighter fit, so inference picks it.
    spec = dict(SPEC)
    spec["customer_old"] = (100, [("customerid", "integer", True)])
    meta = meta_of(spec, declared=[ORDERS_CUST])
    wrong = uid("customer_old", "customerid")
    fn = overlaps({(ORDERS_CUST[0], ORDERS_CUST[1]): (10, 9), (ORDERS_CUST[0], wrong): (10, 10)}, default=(10, 0))
    (r,) = rec.run_recovery("d", meta, 1.0, [0], overlap_fn=fn)
    assert r.recovered == set()
    assert r.wrong_target == [(ORDERS_CUST, wrong)]


def test_run_recovery_counts_extra_for_never_hidden_sources():
    meta = declared_meta(ORDERS_CUST)  # orders.productid was never declared
    (r,) = rec.run_recovery("d", meta, 1.0, [0], overlap_fn=full_overlap())
    assert r.extra == [ORDERS_PROD] and r.recovered == {ORDERS_CUST}


def test_summarize():
    meta = declared_meta(ORDERS_CUST, ORDERS_PROD)
    results = rec.run_recovery("d", meta, 0.5, [0, 1, 2], overlap_fn=full_overlap())
    s = rec.summarize(results)
    assert s["runs"] == 3 and s["hidden_per_run"] == 1
    assert s["recall_mean"] == s["recall_min"] == s["recall_max"] == 1.0
    assert s["ceiling_mean"] == 1.0
    assert s["wrong_target_per_run"] == 0
    assert s["extra_per_run"] == 0
