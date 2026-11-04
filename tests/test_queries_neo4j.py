"""graph/queries.py against a real Neo4j: what ingest writes must read back unchanged.

Same requirements and throwaway-db_id approach as test_ingest_neo4j.py.
"""

import uuid
from pathlib import Path

import pytest

from schemagraph.graph import driver, ingest, queries
from schemagraph.models import DatabaseMetadata

pytestmark = pytest.mark.neo4j

SAMPLE = Path(__file__).parent.parent / "contracts" / "examples" / "metadata.adventureworks.sample.json"


@pytest.fixture(scope="module", autouse=True)
def connected():
    driver.check_connection()
    yield
    driver.close_driver()


@pytest.fixture
def loaded():
    db_id = f"sgtest_{uuid.uuid4().hex[:8]}"
    meta = DatabaseMetadata.model_validate_json(SAMPLE.read_text().replace("adventureworks", db_id))
    ingest.ingest_metadata(meta)
    yield db_id, meta
    ingest.clear_database(db_id)


def test_round_trip_through_neo4j(loaded):
    db_id, meta = loaded
    details = queries.get_all_tables(db_id)
    assert [d.table for d in details] == sorted(meta.tables, key=lambda t: t.uid)
    assert sorted((c for d in details for c in d.columns), key=lambda c: c.uid) == sorted(meta.columns, key=lambda c: c.uid)
    assert sorted((r for d in details for r in d.references), key=lambda r: (r.from_column, r.to_column)) == sorted(
        meta.relationships, key=lambda r: (r.from_column, r.to_column)
    )


def test_get_tables_order_and_unknown(loaded):
    db_id, _ = loaded
    a, b = f"{db_id}:sales.customer", f"{db_id}:person.person"
    got = queries.get_tables(db_id, [a, f"{db_id}:no.such", b])
    assert [d.table.uid for d in got] == [a, b]


def test_list_tables_and_detail(loaded):
    db_id, meta = loaded
    summaries = queries.list_tables(db_id)
    assert len(summaries) == 7 and sum(s.column_count for s in summaries) == len(meta.columns)
    detail = queries.get_table_detail(db_id, f"{db_id}:sales.customer")
    assert len(detail.references) == 1 and len(detail.referenced_by) == 1
    with pytest.raises(queries.TableNotFound):
        queries.get_table_detail(db_id, f"{db_id}:no.such")


def test_reference_edges(loaded):
    db_id, _ = loaded
    assert len(queries.get_reference_edges(db_id)) == 5
    assert len(queries.get_reference_edges(db_id, include_inferred=False)) == 4


def test_other_db_id_is_invisible(loaded):
    assert queries.get_all_tables("sgtest_does_not_exist") == []
