import pytest

from schemagraph.eval.compare import MAX_COLUMN_PAIRINGS, normalize_value, results_match
from schemagraph.models import QueryResult


def qr(cols, rows, truncated=False):
    return QueryResult(columns=cols, rows=rows, row_count=len(rows), truncated=truncated)


# ----------------------------------------------------------------- normalize_value


@pytest.mark.parametrize(
    "a,b",
    [
        ("8248.99", "8248.9900"),
        ("1", "1.0"),
        ("1", "1.000000"),
        ("0", "-0.0"),
        ("1e3", "1000"),
        ("  42 ", "42"),
        ("0.1000000001", "0.1"),            # noise beyond 9 significant digits
        ("123456789.4", "123456789"),
    ],
)
def test_equal_numbers_normalize_alike(a, b):
    assert normalize_value(a) == normalize_value(b)


@pytest.mark.parametrize(
    "a,b",
    [("8248.99", "8248.98"), ("1", "10"), ("0.5", "0.51"), ("abc", "ABC"), ("NaN", "0"), ("1", None), ("", None)],
)
def test_different_values_stay_different(a, b):
    assert normalize_value(a) != normalize_value(b)


def test_null_and_text():
    assert normalize_value(None) is None
    assert normalize_value(" Jon ") == normalize_value("Jon")
    assert normalize_value("Infinity") == ("s", "Infinity")   # a word, not a number
    assert normalize_value("007") == normalize_value("7")     # numeric strings compare as numbers


# ----------------------------------------------------------------- results_match


def test_identical_results_match():
    r = qr(["a", "b"], [["1", "x"], ["2", "y"]])
    assert results_match(r, r, order_matters=False)
    assert results_match(r, r, order_matters=True)


def test_column_names_are_ignored():
    assert results_match(qr(["total"], [["5"]]), qr(["sum"], [["5"]]))


def test_row_order_ignored_unless_it_matters():
    gold = qr(["a"], [["1"], ["2"], ["3"]])
    shuffled = qr(["a"], [["3"], ["1"], ["2"]])
    assert results_match(gold, shuffled, order_matters=False)
    assert not results_match(gold, shuffled, order_matters=True)
    assert results_match(gold, gold, order_matters=True)


def test_duplicate_rows_are_counted():
    assert not results_match(qr(["a"], [["1"], ["1"], ["2"]]), qr(["a"], [["1"], ["2"], ["2"]]))
    assert not results_match(qr(["a"], [["1"], ["1"]]), qr(["a"], [["1"]]))


def test_numbers_and_nulls():
    gold = qr(["n", "t"], [["8248.99", None], ["1", "x"]])
    assert results_match(gold, qr(["n", "t"], [["1.00", "x"], ["8248.9900", None]]))
    assert not results_match(qr(["a"], [[None]]), qr(["a"], [[""]]))   # NULL is not the empty string
    assert not results_match(qr(["a"], [["8248.99"]]), qr(["a"], [["8248.98"]]))
    assert not results_match(qr(["a"], [["8248.99"]]), qr(["a"], [["8249"]]))  # different rounding


def test_text_is_case_sensitive():
    assert not results_match(qr(["a"], [["Jon"]]), qr(["a"], [["jon"]]))


# ----------------------------------------------------------------- column order


def test_swapped_columns_match():
    gold = qr(["firstname", "total"], [["Jon", "5"], ["Eugene", "7"]])
    pred = qr(["t", "n"], [["7", "Eugene"], ["5", "Jon"]])
    assert results_match(gold, pred)


def test_swapped_columns_with_row_order():
    gold = qr(["a", "b"], [["x", "1"], ["y", "2"]])
    pred = qr(["b", "a"], [["1", "x"], ["2", "y"]])
    assert results_match(gold, pred, order_matters=True)
    assert not results_match(gold, qr(["b", "a"], [["2", "y"], ["1", "x"]]), order_matters=True)


def test_columns_cannot_be_recombined_across_rows():
    # the same values per column, but the rows pair them differently
    gold = qr(["a", "b"], [["1", "x"], ["2", "y"]])
    pred = qr(["a", "b"], [["1", "y"], ["2", "x"]])
    assert not results_match(gold, pred)


def test_identical_columns_still_resolve():
    gold = qr(["a", "b", "c"], [["1", "1", "p"], ["2", "2", "q"]])
    assert results_match(gold, qr(["c", "a", "b"], [["p", "1", "1"], ["q", "2", "2"]]))


def test_pairing_search_is_capped():
    # 7 identical columns have 5040 pairings; a wrong answer must stop at the cap, not spin
    n = 7
    gold = qr([f"c{i}" for i in range(n)], [["1"] * n, ["2"] * n])
    pred = qr([f"c{i}" for i in range(n)], [["1"] * n, ["3"] * n])
    assert not results_match(gold, pred)
    assert MAX_COLUMN_PAIRINGS < 5040
    assert results_match(gold, gold)   # the cheap exact test succeeds first


# ----------------------------------------------------------------- shape


def test_different_column_or_row_counts_do_not_match():
    assert not results_match(qr(["a"], [["1"]]), qr(["a", "b"], [["1", "2"]]))
    assert not results_match(qr(["a"], [["1"], ["2"]]), qr(["a"], [["1"]]))


def test_empty_results():
    assert results_match(qr(["a"], []), qr(["b"], []))
    assert not results_match(qr(["a"], []), qr(["a", "b"], []))   # same emptiness, different shape
    assert not results_match(qr(["a"], []), qr(["a"], [["1"]]))


def test_truncated_results_never_match():
    full = qr(["a"], [["1"]])
    cut = qr(["a"], [["1"]], truncated=True)
    assert not results_match(full, cut)
    assert not results_match(cut, full)
    assert not results_match(cut, cut)
