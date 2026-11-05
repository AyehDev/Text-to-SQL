"""Vector search against a real Neo4j (needs SG_TEST_NEO4J=1, NEO4J_PASSWORD, and a
reachable embedding service or the offline WordEmbedder below).

Uses a deterministic bag-of-words embedder so no API key is needed; it checks that
the index DDL, the SET of vectors and the queryNodes Cypher are accepted by Neo4j >= 5.13.
"""

import zlib
from pathlib import Path

import pytest

from schemagraph.extraction import extract
from schemagraph.graph import enrich, ingest, retrieve, vector

pytestmark = pytest.mark.neo4j

SAMPLE = Path(__file__).parent.parent / "contracts" / "examples" / "metadata.adventureworks.sample.json"
DIM = 64


class WordEmbedder:
    def _vec(self, text):
        v = [0.0] * DIM
        for w in "".join(c if c.isalnum() else " " for c in text.lower()).split():
            v[zlib.crc32(w.encode()) % DIM] += 1.0
        return v

    def embed_query(self, text):
        return self._vec(text)

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]


def test_embed_and_retrieve_round_trip():
    meta = extract.load_metadata(SAMPLE)
    ingest.ingest_metadata(meta, rebuild=True)
    vector.drop_vector_indexes()
    vector.create_vector_indexes(DIM)
    emb = WordEmbedder()
    report = enrich.embed_nodes(meta.db_id, emb)
    assert report.tables == len(meta.tables)
    seeds = retrieve.retrieve_seed_tables(meta.db_id, "salesorderheader totaldue", 3, embedder=emb)
    assert seeds and seeds[0].uid.endswith("salesorderheader")
    assert all(s.uid.startswith(f"{meta.db_id}:") for s in seeds)
