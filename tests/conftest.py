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
    if os.environ.get("SG_TEST_PG_DSN"):
        return
    skip = pytest.mark.skip(reason="SG_TEST_PG_DSN not set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def scratch_db_dsn():
    """Create a throwaway database, yield its DSN, drop it afterwards."""
    base = os.environ["SG_TEST_PG_DSN"]
    name = f"sg_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'")
    dsn = make_conninfo(base, dbname=name)
    try:
        yield dsn
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')