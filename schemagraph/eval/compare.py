"""Decide whether a generated query returned the same answer as the gold query (SPEC 5.18).

Results are compared, not SQL text, because many different queries answer the same
question. Two results match when they hold the same rows, with these allowances:

  - column names and aliases are ignored, and so is column order, as long as some
    pairing of the columns makes the rows equal;
  - row order is ignored unless the question asks for an ordering (`order_matters`);
  - numbers compare by value: "8248.9900", "8248.99" and "8.24899e3" are equal, and
    floating-point noise past 9 significant digits is ignored;
  - surrounding whitespace in text is ignored; NULL equals NULL and nothing else.

Not allowed: different row counts, duplicates collapsing (rows are compared as a
multiset), or different rounding (ROUND(x, 2) does not equal x).

A truncated result is never a match, because the rows that were cut off are unknown.
Run gold queries with a row limit large enough that they are not truncated.
"""

from __future__ import annotations

from collections import Counter
from decimal import Context, Decimal, InvalidOperation

from schemagraph.models import QueryResult

SIGNIFICANT_DIGITS = 9
MAX_COLUMN_PAIRINGS = 1000  # cap on the column permutations tried for one pair of results

_CONTEXT = Context(prec=SIGNIFICANT_DIGITS)

Cell = tuple[str, str] | None  # (kind, value); None is SQL NULL
Row = tuple[Cell, ...]


def normalize_value(value: str | None) -> Cell:
    """Make one cell comparable. Numbers become a canonical string, text is stripped."""
    if value is None:
        return None
    text = value.strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        return ("s", text)
    if not number.is_finite():  # 'NaN' and 'Infinity' are words here, not numbers
        return ("s", text)
    number = _CONTEXT.plus(number)  # round to SIGNIFICANT_DIGITS
    if number == 0:
        return ("n", "0")  # also folds -0
    return ("n", format(number.normalize(), "f"))


def _sort_key(cell: Cell) -> tuple[int, str, str]:
    return (0, "", "") if cell is None else (1, cell[0], cell[1])


def _columns(rows: list[Row], width: int) -> list[Counter]:
    return [Counter(row[i] for row in rows) for i in range(width)]


def _signature(column: Counter) -> tuple:
    return tuple(sorted(column.items(), key=lambda kv: (_sort_key(kv[0]), kv[1])))


def _pairings(gold_sigs: list[tuple], pred_sigs: list[tuple]):
    """Ways to line predicted columns up with gold columns: each gold column takes a
    predicted column holding exactly the same multiset of values."""
    n = len(gold_sigs)
    used = [False] * n

    def extend(i: int, chosen: list[int]):
        if i == n:
            yield list(chosen)
            return
        for j in range(n):
            if not used[j] and pred_sigs[j] == gold_sigs[i]:
                used[j] = True
                chosen.append(j)
                yield from extend(i + 1, chosen)
                chosen.pop()
                used[j] = False

    return extend(0, [])


def results_match(gold: QueryResult, predicted: QueryResult, order_matters: bool = False) -> bool:
    """True if `predicted` returned the same answer as `gold` (see the module docstring)."""
    if gold.truncated or predicted.truncated:
        return False
    if len(gold.columns) != len(predicted.columns):
        return False

    width = len(gold.columns)
    gold_rows = [tuple(normalize_value(v) for v in row) for row in gold.rows]
    pred_rows = [tuple(normalize_value(v) for v in row) for row in predicted.rows]
    if len(gold_rows) != len(pred_rows):
        return False
    if not gold_rows or width == 0:
        return True  # same shape, both empty

    # Cheap exact test first: same columns in the same order.
    if _same(gold_rows, pred_rows, order_matters):
        return True

    gold_sigs = [_signature(c) for c in _columns(gold_rows, width)]
    pred_sigs = [_signature(c) for c in _columns(pred_rows, width)]
    if Counter(gold_sigs) != Counter(pred_sigs):
        return False  # no column pairing can work

    for tried, order in enumerate(_pairings(gold_sigs, pred_sigs)):
        if tried >= MAX_COLUMN_PAIRINGS:
            return False
        permuted = [tuple(row[j] for j in order) for row in pred_rows]
        if _same(gold_rows, permuted, order_matters):
            return True
    return False


def _same(gold_rows: list[Row], pred_rows: list[Row], order_matters: bool) -> bool:
    if order_matters:
        return gold_rows == pred_rows
    return Counter(gold_rows) == Counter(pred_rows)
