"""Embedding providers (OpenAI, Voyage/Anthropic), named vector sets, and the config that selects them.

No network and no Neo4j: HTTP goes to a local server, Neo4j to FakeVectorStore.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import ValidationError

from schemagraph import config as cfg
from schemagraph.config import EmbeddingConfig
from schemagraph.graph import enrich, retrieve, vector
from tests.test_retrieve_unit import DB, DIM, FakeVectorStore, WordEmbedder, _details, meta  # noqa: F401


# ----------------------------------------------------------------- config


def make_cfg(**kw):
    kw.setdefault("model", "m")
    kw.setdefault("dimensions", 8)
    return EmbeddingConfig(**kw)


def test_openai_is_the_default_provider():
    c = make_cfg()
    assert (c.provider, c.name) == ("openai", "default")
    assert c.base_url == "https://api.openai.com/v1" and c.api_key_env == "OPENAI_API_KEY"


def test_voyage_defaults():
    c = make_cfg(provider="voyage")
    assert c.provider == "voyage" and c.api_key_env == "VOYAGE_API_KEY"
    assert c.base_url == "https://ai.mongodb.com/v1"


def test_anthropic_means_voyage():
    c = make_cfg(provider="anthropic")
    assert c.provider == "voyage" and c.api_key_env == "VOYAGE_API_KEY"


def test_explicit_settings_beat_provider_defaults():
    c = make_cfg(provider="voyage", base_url="https://api.voyageai.com/v1", api_key_env="MY_KEY")
    assert (c.base_url, c.api_key_env) == ("https://api.voyageai.com/v1", "MY_KEY")


def test_explicit_null_key_means_no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    c = make_cfg(base_url="http://localhost:11434/v1", api_key_env=None)
    assert c.api_key_env is None and c.api_key is None


def test_missing_key_variable_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="VOYAGE_API_KEY"):
        make_cfg(provider="voyage").api_key


@pytest.mark.parametrize("bad", ["cohere", "", "OpenAI"])
def test_unknown_provider_is_rejected(bad):
    with pytest.raises(ValidationError):
        make_cfg(provider=bad)


@pytest.mark.parametrize("bad", ["Upper", "1abc", "", "a b", "x; DROP INDEX y", "a" * 40, "-x"])
def test_bad_profile_names_are_rejected(bad):
    with pytest.raises(ValidationError):
        make_cfg(name=bad)


def test_config_file_section_loads(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(
        "postgres:\n  admin: {user: a, password_env: A}\n  readonly: {user: r, password_env: R}\n"
        "embedding:\n  provider: anthropic\n  name: voyage\n  model: voyage-4\n  dimensions: 1024\n"
    )
    c = cfg.load_config(path).embedding
    assert (c.provider, c.name, c.model, c.dimensions) == ("voyage", "voyage", "voyage-4", 1024)


# ----------------------------------------------------------------- names


def test_default_names_are_the_plain_ones():
    assert vector.embedding_names() == ("table_embedding", "column_embedding", "embedding")


def test_other_names_add_a_suffix():
    assert vector.embedding_names("voyage") == ("table_embedding_voyage", "column_embedding_voyage", "embedding_voyage")


@pytest.mark.parametrize("bad", ["", "Upper", "x y", "a;b", "1a", None, "a" * 40])
def test_names_are_checked_before_they_reach_cypher(bad):
    with pytest.raises(ValueError):
        vector.embedding_names(bad)


def test_names_for_prefers_override_then_embedder_then_default():
    class Named:
        name = "voyage"

    assert vector.names_for(Named()).prop == "embedding_voyage"
    assert vector.names_for(Named(), "other").prop == "embedding_other"
    assert vector.names_for(object()).prop == "embedding"


class Recorder:
    def __init__(self):
        self.statements = []

    def run(self, statement, **params):
        self.statements.append(statement)
        return self

    def consume(self):
        return None


def test_index_statements_use_the_name_and_dimension():
    t, c = vector.index_statements(1024, "voyage")
    assert "table_embedding_voyage" in t and "(n.embedding_voyage)" in t and "`vector.dimensions`: 1024" in t
    assert "column_embedding_voyage" in c and "FOR (n:Column)" in c
    assert "table_embedding " in vector.index_statements(8)[0]


def test_create_and_drop_touch_only_the_named_indexes():
    rec = Recorder()
    vector.create_vector_indexes(8, rec, name="voyage")
    vector.drop_vector_indexes(rec, name="voyage")
    assert len(rec.statements) == 4
    assert all("voyage" in s for s in rec.statements)
    assert rec.statements[2:] == ["DROP INDEX table_embedding_voyage IF EXISTS", "DROP INDEX column_embedding_voyage IF EXISTS"]


# ----------------------------------------------------------------- HTTP providers


class Handler(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen.append((self.path, self.headers.get("Authorization"), body))
        n = len(body["input"])
        dims = body.get("output_dimension") or body.get("dimensions") or 3
        raw = json.dumps({"data": [{"index": i, "embedding": [0.5] * dims} for i in range(n)]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    Handler.seen = []
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_voyage_marks_queries_and_documents(server, monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    emb = vector.make_embedder(make_cfg(provider="voyage", base_url=server, dimensions=3, model="voyage-4"))
    assert isinstance(emb, vector.VoyageEmbedder)
    emb.embed_documents(["a", "b"])
    emb.embed_query("q")
    (p1, auth1, b1), (p2, _, b2) = Handler.seen
    assert p1 == "/v1/embeddings" and auth1 == "Bearer vk"
    assert (b1["model"], b1["input"], b1["input_type"]) == ("voyage-4", ["a", "b"], "document")
    assert (b2["input"], b2["input_type"]) == (["q"], "query")
    assert "dimensions" not in b1 and "output_dimension" not in b1


def test_voyage_shortens_with_output_dimension(server, monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    emb = vector.make_embedder(make_cfg(provider="anthropic", base_url=server, dimensions=5, send_dimensions=True))
    assert len(emb.embed_query("q")) == 5
    body = Handler.seen[0][2]
    assert body["output_dimension"] == 5 and "dimensions" not in body


def test_openai_payload_has_no_input_type(server, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ok")
    emb = vector.make_embedder(make_cfg(base_url=server, dimensions=7, send_dimensions=True))
    assert isinstance(emb, vector.OpenAICompatibleEmbedder)
    emb.embed_documents(["a"])
    emb.embed_query("q")
    for _, auth, body in Handler.seen:
        assert auth == "Bearer ok" and "input_type" not in body and body["dimensions"] == 7


def test_dimension_mismatch_says_how_to_fix_it(server, monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    emb = vector.make_embedder(make_cfg(provider="voyage", base_url=server, dimensions=9))  # server returns 3
    with pytest.raises(vector.EmbeddingError, match="send_dimensions"):
        emb.embed_query("q")


def test_embedders_carry_their_profile_name(server, monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    assert vector.make_embedder(make_cfg(provider="voyage", name="v", base_url=server)).name == "v"
    assert vector.make_embedder(make_cfg(api_key_env=None)).name == "default"


def test_get_embedder_follows_the_config(monkeypatch):
    class App:
        embedding = make_cfg(provider="anthropic")

    monkeypatch.setattr(vector, "get_config", lambda: App())
    assert isinstance(vector.get_embedder(), vector.VoyageEmbedder)

    class NoEmbedding:
        embedding = None

    monkeypatch.setattr(vector, "get_config", lambda: NoEmbedding())
    with pytest.raises(RuntimeError, match="embedding"):
        vector.get_embedder()


# ----------------------------------------------------------------- two models in one graph


class SeedEmbedder(WordEmbedder):
    """A word embedder whose hashing differs by seed, standing in for a second model."""

    def __init__(self, name, seed):
        super().__init__()
        self.name, self._seed = name, seed

    def _vec(self, text):
        import zlib

        v = [0.0] * DIM
        for w in "".join(c if c.isalnum() else " " for c in text.lower()).split():
            v[zlib.crc32(f"{self._seed}{w}".encode()) % DIM] += 1.0
        return v


def test_two_models_live_side_by_side(meta, monkeypatch):
    store = FakeVectorStore()
    store.add_metadata(meta)
    monkeypatch.setattr(enrich.queries, "get_all_tables", lambda db_id, s: _details(meta))
    a, b = SeedEmbedder("default", "a"), SeedEmbedder("voyage", "b")
    enrich.embed_nodes(DB, a, store)
    enrich.embed_nodes(DB, b, store)

    node = next(iter(store.tables.values()))
    assert "embedding" in node and "embedding_voyage" in node and node["embedding"] != node["embedding_voyage"]

    # each model is searched through its own index, with its own query vector
    store.queries.clear()
    retrieve.retrieve_seed_tables(DB, "customer", 3, embedder=a, session=store)
    retrieve.retrieve_seed_tables(DB, "customer", 3, embedder=b, session=store)
    indexes = [p["index"] for _, p in store.queries if "index" in p]
    assert indexes == ["table_embedding", "column_embedding", "table_embedding_voyage", "column_embedding_voyage"]


def test_searching_a_name_that_was_never_embedded_finds_nothing(meta, monkeypatch):
    store = FakeVectorStore()
    store.add_metadata(meta)
    monkeypatch.setattr(enrich.queries, "get_all_tables", lambda db_id, s: _details(meta))
    enrich.embed_nodes(DB, SeedEmbedder("default", "a"), store)
    assert retrieve.retrieve_seed_tables(DB, "customer", 3, embedder=SeedEmbedder("other", "a"), session=store) == []


def test_explicit_name_overrides_the_embedders(meta, monkeypatch):
    store = FakeVectorStore()
    store.add_metadata(meta)
    monkeypatch.setattr(enrich.queries, "get_all_tables", lambda db_id, s: _details(meta))
    emb = SeedEmbedder("default", "a")
    enrich.embed_nodes(DB, emb, store, name="trial")
    assert "embedding_trial" in next(iter(store.tables.values()))
    assert retrieve.retrieve_seed_tables(DB, "customer", 3, embedder=emb, session=store, name="trial")
