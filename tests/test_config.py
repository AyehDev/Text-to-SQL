"""Shared fixtures.

Integration tests need a PostgreSQL server you are allowed to create databases
on. Point SG_TEST_PG_DSN at it, e.g.

    export SG_TEST_PG_DSN="host=127.0.0.1 port=5432 user=postgres dbname=postgres"

Without it, tests marked `integration` are skipped, so plain `pytest` still
works on a laptop with no database.
"""

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_collection_modifyitems(config, items):
    no_dsn = pytest.mark.skip(reason="SG_TEST_PG_DSN not set")
    no_aw = pytest.mark.skip(reason="SG_TEST_ADVENTUREWORKS not set")
    no_neo = pytest.mark.skip(reason="SG_TEST_NEO4J not set")
    for item in items:
        if "integration" in item.keywords and not os.environ.get("SG_TEST_PG_DSN"):
            item.add_marker(no_dsn)
        if "adventureworks" in item.keywords and not os.environ.get("SG_TEST_ADVENTUREWORKS"):
            item.add_marker(no_aw)
        if "neo4j" in item.keywords and not os.environ.get("SG_TEST_NEO4J"):
            item.add_marker(no_neo)


@pytest.fixture(scope="session")
def fixture_dsn(scratch_db_dsn):
    """The scratch database with tests/fixtures/extraction_fixture.sql loaded once."""
    with psycopg.connect(scratch_db_dsn, autocommit=True) as c:
        c.execute((FIXTURES / "extraction_fixture.sql").read_text())
    return scratch_db_dsn


def _create_scratch_db():
    """Create a throwaway UTF8 database; return (server DSN, database name, its DSN)."""
    base = os.environ["SG_TEST_PG_DSN"]
    name = f"sg_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'")
    return base, name, make_conninfo(base, dbname=name)


def _drop_scratch_db(base, name):
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def scratch_db_dsn():
    """Create a throwaway database, yield its DSN, drop it afterwards."""
    base, name, dsn = _create_scratch_db()
    try:
        yield dsn
    finally:
        _drop_scratch_db(base, name)


@pytest.fixture(scope="session")
def infer_dsn():
    """A second scratch database holding tests/fixtures/infer_fixture.sql."""
    base, name, dsn = _create_scratch_db()
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute((FIXTURES / "infer_fixture.sql").read_text())
        yield dsn
    finally:
        _drop_scratch_db(base, name)