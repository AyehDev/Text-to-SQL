"""Write embeddings onto the graph's Table and Column nodes.

`embed_nodes` reads every table and column of one database back from the graph,
builds the text for each (graph/vector.py), embeds it, and stores the vector in
the `embedding` property the vector indexes cover. Re-running it replaces the
vectors, so it is safe after descriptions change. (Generating missing
descriptions with an LLM is a separate step, not written yet.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from schemagraph.graph import queries, vector
from schemagraph.graph.cypher import use_session, write

log = logging.getLogger(__name__)

def set_embeddings_query(label: str, prop: str) -> str:
    """`prop` comes from vector.embedding_names, which only produces checked identifiers."""
    return f"UNWIND $rows AS row MATCH (n:{label} {{uid: row.uid}}) SET n.{prop} = row.vec"


@dataclass(frozen=True)
class EmbedReport:
    tables: int
    columns: int


def embedding_inputs(details: list, include_generated: bool = True) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(uid, text) pairs for tables and for columns. Pure."""
    tables: list[tuple[str, str]] = []
    columns: list[tuple[str, str]] = []
    for d in details:
        tables.append((d.table.uid, vector.table_text(d.table, include_generated)))
        for c in d.columns:
            columns.append((c.uid, vector.column_text(d.table, c, include_generated)))
    return tables, columns


def _store(session: Any, label: str, prop: str, items: list[tuple[str, str]], embedder: vector.Embedder, batch_size: int) -> None:
    if not items:
        return
    vectors = embedder.embed_documents([text for _, text in items])
    if len(vectors) != len(items):
        raise vector.EmbeddingError(f"embedder returned {len(vectors)} vectors for {len(items)} texts")
    rows = [{"uid": uid, "vec": vec} for (uid, _), vec in zip(items, vectors, strict=True)]
    write(session, set_embeddings_query(label, prop), rows, batch_size)


def embed_nodes(
    db_id: str,
    embedder: vector.Embedder | None = None,
    session: Any | None = None,
    include_generated: bool = True,
    batch_size: int = 500,
    name: str | None = None,
) -> EmbedReport:
    """Embed every table and column of `db_id` and store the vectors on the nodes.

    include_generated=False leaves LLM-written descriptions out of the text, to
    measure what they add to retrieval. The vectors are stored under the embedder's
    `name` (or the `name` given here), so several models can be embedded into one graph.
    """
    embedder = embedder or vector.get_embedder()
    names = vector.names_for(embedder, name)
    with use_session(session) as s:
        details = queries.get_all_tables(db_id, s)
        tables, columns = embedding_inputs(details, include_generated)
        _store(s, "Table", names.prop, tables, embedder, batch_size)
        _store(s, "Column", names.prop, columns, embedder, batch_size)
    log.info("embedded %d tables and %d columns for %s", len(tables), len(columns), db_id)
    return EmbedReport(len(tables), len(columns))
