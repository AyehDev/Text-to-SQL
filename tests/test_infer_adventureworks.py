"""Inference acceptance on the real AdventureWorks database.

Needs SG_TEST_ADVENTUREWORKS=1, the database loaded as `adventureworks` (see
scripts/load_adventureworks.sh), and the sg_admin role configured.

AdventureWorks declares almost every foreign key, so recall is measured by
hiding declared ones. The rules were tuned on this database: these numbers
are an optimistic regression guard, not a general accuracy claim.
"""

import pytest

from schemagraph.eval import infer_recovery as rec
from schemagraph.extraction import extract as ex
from schemagraph.extraction import infer

pytestmark = pytest.mark.adventureworks

DB = "adventureworks"
SEEDS = list(range(5))


@pytest.fixture(scope="module")
def meta():
    return ex.extract_metadata(DB, sample_n=0)


def short(uid):
    return uid.split(":", 1)[1]


def test_undeclared_relationships_found_are_exactly_the_known_two(meta):
    found = {(short(r.from_column), short(r.to_column)) for r in infer.infer_relationships(DB, meta)}
    assert found == {
        ("production.transactionhistoryarchive.productid", "production.product.productid"),
        ("production.workorderrouting.productid", "production.product.productid"),
    }


def test_with_inferred_keeps_integrity(meta):
    out = infer.with_inferred(DB, meta)
    assert len([r for r in out.relationships if r.source == "inferred"]) == 2
    assert len([r for r in out.relationships if r.source == "declared"]) == len(
        [r for r in meta.relationships if r.source == "declared"]
    )


def test_recovery_of_30_percent_hidden(meta):
    results = rec.run_recovery(DB, meta, 0.3, SEEDS)
    s = rec.summarize(results)
    assert s["recall_mean"] >= 0.95
    assert s["wrong_target_per_run"] <= 0.5
    extras = {short(f) + " -> " + short(t) for r in results for f, t in r.extra}
    assert extras == {
        "production.transactionhistoryarchive.productid -> production.product.productid",
        "production.workorderrouting.productid -> production.product.productid",
    }


def test_blind_recovery_of_all_declared(meta):
    # every single-column FK hidden at once: the hardest case, and the one where
    # identical key sets (person.person vs person.password) cannot be told apart
    (r,) = rec.run_recovery(DB, meta, 1.0, [0])
    assert r.recall >= 0.9
    assert len(r.wrong_target) <= 4  # regression guard on the known ties