"""Small helpers shared by every module that talks to Neo4j.

Each function takes an optional `session` (a neo4j Session, or a fake in tests)
and otherwise opens one from graph.driver, so callers can group several
operations in one session or just call them.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def use_session(session: Any | None) -> Iterator[Any]:
    """The caller's session, or a fresh one that is closed afterwards."""
    if session is not None:
        yield session
        return
    from schemagraph.graph import driver  # imported here so tests need no neo4j server

    with driver.session() as s:
        yield s


def batches(rows: list, size: int) -> Iterable[list]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def write(session: Any, query: str, rows: list, batch_size: int) -> None:
    """Run one statement over the rows, one transaction per batch."""

    def work(tx: Any, batch: list) -> None:
        tx.run(query, rows=batch).consume()

    for batch in batches(rows, batch_size):
        session.execute_write(work, batch)


def read(session: Any, query: str, **params: Any) -> list[dict[str, Any]]:
    """Run a read query and return its records as plain dicts."""

    def work(tx: Any) -> list[dict[str, Any]]:
        return [record.data() for record in tx.run(query, **params)]

    return session.execute_read(work)