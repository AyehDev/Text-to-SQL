"""graph/ingest.py against a real Neo4j.

Needs a running Neo4j and SG_TEST_NEO4J=1, with `neo4j:` in the config named by
SCHEMAGRAPH_CONFIG and the password variable exported. The tests use a copy of
the sample metadata under a throwaway db_id (sgtest_<random>) and delete it
afterwards, so a real graph in the same Neo4j is never touched.
"""

import uuid
from pathlib import Path

import pytest

from schemagraph.models import DatabaseMetadata
from schemagraph.graph import driver, ingest

pytestmark = pytest.mark.neo4j

SAMPLE = Path(__file__).parent.parent / "contracts" / "examples" / "metadata.adventureworks.sample.json"


def renamed(db_id: str):
    text = SAMPLE.read_text().replace("adventureworks", db_id)
    return DatabaseMetadata.model_validate_json(text)


@pytest.fixture(scope="module", autouse=True)
def connected():
    driver.check_connection()
    yield
    driver.close_driver()


@pytest.fixture
def db_id():
    name = f"sgtest_{uuid.uuid4().hex[:8]}"
    yield name
    ingest.clear_database(name)


def counts(db_id):
    with driver.session() as s:
        return {
            label: s.run(f"MATCH (n:{label} {{db_id: $d}}) RETURN count(n) AS n", d=db_id).single()["n"]
            for label in ("Schema", "Table", "Column")
        } | {
            "REFERENCES": s.run(
                "MATCH (:Column {db_id: $d})-[r:REFERENCES]->() RETURN count(r) AS n", d=db_id
            ).single()["n"]
        }


def test_ingest_matches_metadata(db_id):
    meta = renamed(db_id)
    report = ingest.ingest_metadata(meta)
    assert report.tables == 7
    assert counts(db_id) == {"Schema": 3, "Table": 7, "Column": 93, "REFERENCES": 5}
    assert ingest.verify_against_metadata(meta) == []


def test_ingest_twice_gives_the_same_graph(db_id):
    meta = renamed(db_id)
    ingest.ingest_metadata(meta)
    before = counts(db_id)
    ingest.ingest_metadata(meta)
    assert counts(db_id) == before
    assert ingest.verify_against_metadata(meta) == []


def test_inferred_edge_keeps_evidence_and_weights(db_id):
    ingest.ingest_metadata(renamed(db_id))
    with driver.session() as s:
        r = s.run(
            "MATCH (:Column {db_id: $d})-[r:REFERENCES {source: 'inferred'}]->() RETURN r", d=db_id
        ).single()["r"]
    assert '"value_overlap"' in r["evidence_json"]
    assert r["weight_forward"] > 1.0 and r["weight_reverse"] > r["weight_forward"]


def test_rebuild_removes_what_the_source_no_longer_has(db_id):
    meta = renamed(db_id)
    ingest.ingest_metadata(meta)
    smaller = meta.model_copy(update={
        "tables": meta.tables[:-1],
        "columns": [c for c in meta.columns if c.table_uid != meta.tables[-1].uid],
        "relationships": [],
    })
    ingest.ingest_metadata(smaller, rebuild=True)
    assert counts(db_id)["Table"] == 6
    assert ingest.verify_against_metadata(smaller) == []


def test_clear_leaves_other_databases_alone(db_id):
    other = f"{db_id}_other"
    try:
        ingest.ingest_metadata(renamed(db_id))
        ingest.ingest_metadata(renamed(other))
        ingest.clear_database(db_id)
        assert counts(db_id) == {"Schema": 0, "Table": 0, "Column": 0, "REFERENCES": 0}
        assert counts(other)["Table"] == 7
    finally:
        ingest.clear_database(other)


def test_apply_weights_repairs_changed_weights(db_id):
    meta = renamed(db_id)
    ingest.ingest_metadata(meta)
    with driver.session() as s:
        s.run("MATCH (:Column {db_id: $d})-[r:REFERENCES]->() SET r.weight_forward = 99", d=db_id).consume()
    assert ingest.verify_against_metadata(meta)            # the damage is detected
    assert ingest.apply_weights(db_id) == 5
    assert ingest.verify_against_metadata(meta) == []
