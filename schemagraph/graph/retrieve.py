"""Find the tables most related to a question (SPEC 5.9).

The question is embedded once and searched against two vector indexes: one over
tables and one over columns. A column hit counts for its table, so a table is
found whether the question mentions it or only one of its columns. Each table
keeps its best score; `matched_on` says where that score came from.

The indexes hold every database in one place, so each search asks for more
candidates than needed and keeps only those with this database's db_id.
"""

from __future__ import annotations

from typing import Any

from schemagraph.graph import vector
from schemagraph.graph.cypher import read, use_session
from schemagraph.models import SeedTable

DEFAULT_CANDIDATES = 100
COLUMN_FACTOR = 3  # a table has several columns, so over-fetch column hits

TABLE_HITS = (
    "CALL db.index.vector.queryNodes($index, $n, $vector) YIELD node, score "
    "WHERE node.db_id = $db_id "
    "RETURN node.uid AS uid, score"
)
COLUMN_HITS = (
    "CALL db.index.vector.queryNodes($index, $n, $vector) YIELD node, score "
    "WHERE node.db_id = $db_id "
    "MATCH (t:Table)-[:HAS_COLUMN]->(node) "
    "RETURN t.uid AS uid, node.name AS column, score"
)


def rollup(table_hits: list[dict], column_hits: list[dict], k: int) -> list[SeedTable]:
    """Best score per table, ties broken by uid, top k. Pure.

    table_hits: {"uid", "score"}; column_hits: {"uid" (the table's), "column", "score"}.
    A table whose own score is at least as high as every column's is matched on "table".
    """
    if k <= 0:
        return []
    best: dict[str, tuple[float, str]] = {}
    for hit in sorted(table_hits, key=lambda h: (-h["score"], h["uid"])):
        best.setdefault(hit["uid"], (float(hit["score"]), "table"))
    for hit in sorted(column_hits, key=lambda h: (-h["score"], h["column"])):
        score = float(hit["score"])
        if hit["uid"] not in best or score > best[hit["uid"]][0]:
            best[hit["uid"]] = (score, f"column:{hit['column']}")
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return [SeedTable(uid=uid, score=round(score, 4), matched_on=on) for uid, (score, on) in ranked[:k]]


def retrieve_seed_tables(
    db_id: str,
    question: str,
    k: int,
    *,
    embedder: vector.Embedder | None = None,
    session: Any | None = None,
    candidates: int = DEFAULT_CANDIDATES,
    min_score: float | None = None,
) -> list[SeedTable]:
    """The k tables most related to `question`, best first. Fewer if the database has fewer.

    min_score drops weaker hits (Neo4j cosine scores run 0 to 1). Raises
    EmbeddingError if the embedding service fails; an empty question gives [].
    """
    if k <= 0 or not question.strip():
        return []
    embedder = embedder or vector.get_embedder()
    vec = embedder.embed_query(question)
    with use_session(session) as s:
        tables = read(s, TABLE_HITS, index=vector.TABLE_INDEX, n=candidates, vector=vec, db_id=db_id)
        columns = read(s, COLUMN_HITS, index=vector.COLUMN_INDEX, n=candidates * COLUMN_FACTOR, vector=vec, db_id=db_id)
    seeds = rollup(tables, columns, k)
    if min_score is not None:
        seeds = [s_ for s_ in seeds if s_.score >= min_score]
    return seeds
