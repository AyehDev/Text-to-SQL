import pytest

from schemagraph.models import ExplainResult
from schemagraph.pipeline import validate as validate_module
from schemagraph.sql_validation import is_single_select


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT 1",
        "SELECT * FROM customers WHERE id = 1;",
        "WITH totals AS (SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id) "
        "SELECT * FROM totals",
        "SELECT id FROM customers UNION SELECT customer_id FROM orders",
        "-- query comment\nSELECT 'DELETE FROM customers' AS example",
    ],
)
def test_is_single_select_accepts_readonly_queries(statement):
    assert is_single_select(statement) is True


@pytest.mark.parametrize(
    "statement",
    [
        "",
        "SELECT * FROM",
        "INSERT INTO customers VALUES (1)",
        "UPDATE customers SET name = 'x'",
        "DELETE FROM customers",
        "SELECT 1; SELECT 2",
        "SELECT * INTO copied_customers FROM customers",
        "WITH changed AS (DELETE FROM customers RETURNING *) SELECT * FROM changed",
        "SELECT * FROM customers FOR UPDATE",
    ],
)
def test_is_single_select_rejects_unsafe_or_invalid_sql(statement):
    assert is_single_select(statement) is False


def test_is_single_select_rejects_missing_sql():
    assert is_single_select(None) is False


def test_validate_rejects_unsafe_sql_without_explaining(monkeypatch):
    def unexpected_explain(*args, **kwargs):
        pytest.fail("Unsafe SQL must be rejected before EXPLAIN")

    monkeypatch.setattr(validate_module.db, "explain", unexpected_explain)

    result = validate_module.validate("adventureworks", "DELETE FROM customers")

    assert result.ok is False
    assert result.error == "Only a single read-only SELECT statement is allowed."


def test_validate_explains_a_safe_select(monkeypatch):
    calls = []

    def fake_explain(db_id, statement):
        calls.append((db_id, statement))
        return ExplainResult(ok=True)

    monkeypatch.setattr(validate_module.db, "explain", fake_explain)

    result = validate_module.validate("adventureworks", "SELECT 1")

    assert result.ok is True
    assert calls == [("adventureworks", "SELECT 1")]
