"""One shared Neo4j driver for every graph module (ingest, retrieve, planner, queries).

The driver is created on first use and reused: it owns a connection pool, so
making one per call would be slow and leak connections. Creating it does not
connect; call `check_connection()` to fail early with a readable message.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from neo4j import Driver, GraphDatabase, Session

from schemagraph.config import Neo4jConfig, get_config


def neo4j_config() -> Neo4jConfig:
    cfg = get_config().neo4j
    if cfg is None:
        raise RuntimeError(
            "config.yml has no 'neo4j' section (uri, user, password_env, database); "
            "see config.example.yml"
        )
    return cfg


@lru_cache(maxsize=1)
def get_driver() -> Driver:
    cfg = neo4j_config()
    return GraphDatabase.driver(cfg.uri, auth=(cfg.user, cfg.password.get_secret_value()))


@contextmanager
def session() -> Iterator[Session]:
    """A session on the configured database."""
    with get_driver().session(database=neo4j_config().database) as s:
        yield s


def check_connection() -> None:
    """Raise with a clear message if Neo4j is unreachable or the login is wrong."""
    cfg = neo4j_config()
    try:
        get_driver().verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"cannot connect to Neo4j at {cfg.uri} as {cfg.user!r}: {exc}") from exc


def close_driver() -> None:
    """Close the shared driver (end of a CLI run, or between tests)."""
    if get_driver.cache_info().currsize:
        get_driver().close()
        get_driver.cache_clear()