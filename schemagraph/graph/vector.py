"""Everything embedding-related that retrieval and enrichment share.

- the `Embedder` interface (the same two methods as LangChain's `Embeddings`, so
  a LangChain embeddings object can be passed anywhere an Embedder is wanted)
- `OpenAICompatibleEmbedder`, a small HTTP client for any OpenAI-style /embeddings endpoint
- the text that gets embedded for a table and for a column (pure functions)
- creating and dropping the two Neo4j vector indexes

Retrieval finds tables two ways: through the table's own text (name and
description) and through its columns, so a question about "total due" can find
`salesorderheader` through its `totaldue` column. Both live in the same Neo4j
database, kept apart by the db_id property on every node.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from schemagraph.config import EmbeddingConfig, get_config
from schemagraph.graph.cypher import use_session
from schemagraph.models import ColumnMeta, TableMeta

log = logging.getLogger(__name__)

TABLE_INDEX = "table_embedding"
COLUMN_INDEX = "column_embedding"
EMBEDDING_PROP = "embedding"

MAX_SAMPLE_VALUES = 3
MAX_SAMPLE_CHARS = 40


class EmbeddingError(RuntimeError):
    """The embedding service failed or returned something unusable."""


class Embedder(Protocol):
    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------------------
# Text that gets embedded (pure)
# ---------------------------------------------------------------------------


def humanize(name: str) -> str:
    """'order_detail' -> 'order detail'. Names without underscores are left alone."""
    return name.replace("_", " ")


def _description(description: str | None, source: str | None, include_generated: bool) -> str | None:
    if not description or not description.strip():
        return None
    if source == "generated" and not include_generated:
        return None  # the ablation: embed only what the catalog itself says
    return " ".join(description.split())


def table_text(table: TableMeta, include_generated: bool = True) -> str:
    """'sales.salesorderheader: General sales order information.'"""
    text = f"{table.schema_}.{table.name}"
    if humanize(table.name) != table.name:
        text += f" ({humanize(table.name)})"
    desc = _description(table.description, table.description_source, include_generated)
    return f"{text}: {desc}" if desc else text


def column_text(table: TableMeta, column: ColumnMeta, include_generated: bool = True) -> str:
    """'sales.salesorderheader.totaldue (numeric): Total due from the customer. Examples: 8248.99, 6383.66'"""
    text = f"{table.schema_}.{table.name}.{column.name}"
    if humanize(column.name) != column.name:
        text += f" ({humanize(column.name)})"
    text += f" ({column.data_type})"
    desc = _description(column.description, column.description_source, include_generated)
    if desc:
        text += f": {desc}"
    samples = [v[:MAX_SAMPLE_CHARS] for v in column.sample_values[:MAX_SAMPLE_VALUES] if v]
    if samples:
        text += f". Examples: {', '.join(samples)}"
    return text


# ---------------------------------------------------------------------------
# Embedding over HTTP
# ---------------------------------------------------------------------------


class OpenAICompatibleEmbedder:
    """Calls POST {base_url}/embeddings. Works with OpenAI and servers that copy its API."""

    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 3,
    ) -> None:
        self.config = config
        self._sleep = sleep
        self._retries = retries

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        size = self.config.batch_size
        for i in range(0, len(texts), size):
            out.extend(self._embed_batch(texts[i : i + size]))
        return out

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {"model": self.config.model, "input": texts}
        if self.config.send_dimensions:
            payload["dimensions"] = self.config.dimensions
        headers = {"Content-Type": "application/json"}
        key = self.config.api_key
        if key is not None:
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/embeddings",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        body = self._send(request)
        try:
            data = sorted(body["data"], key=lambda item: item["index"])
            vectors = [[float(x) for x in item["embedding"]] for item in data]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"unexpected response from the embedding service: {str(body)[:200]}") from exc
        if len(vectors) != len(texts):
            raise EmbeddingError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
        for v in vectors:
            if len(v) != self.config.dimensions:
                raise EmbeddingError(
                    f"the model returned {len(v)}-dimensional vectors but config says dimensions: "
                    f"{self.config.dimensions}"
                )
        return vectors

    def _send(self, request: urllib.request.Request) -> Any:
        for attempt in range(self._retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_s) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:200].decode(errors="replace")
                if exc.code in self.RETRY_STATUS and attempt < self._retries:
                    self._sleep(2**attempt)
                    continue
                raise EmbeddingError(f"embedding service returned HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self._retries:
                    self._sleep(2**attempt)
                    continue
                raise EmbeddingError(f"cannot reach the embedding service at {self.config.base_url}: {exc}") from exc
        raise AssertionError("unreachable")  # pragma: no cover


def get_embedder() -> Embedder:
    """The embedder named by the `embedding:` section of config.yml."""
    cfg = get_config().embedding
    if cfg is None:
        raise RuntimeError(
            "config.yml has no 'embedding' section (model, dimensions, ...); see config.example.yml"
        )
    return OpenAICompatibleEmbedder(cfg)


# ---------------------------------------------------------------------------
# Neo4j vector indexes
# ---------------------------------------------------------------------------


def index_statements(dimensions: int) -> list[str]:
    """Cypher to create both indexes. The dimension is checked and written into the text
    because index options cannot always be passed as parameters."""
    if not isinstance(dimensions, int) or isinstance(dimensions, bool) or not 1 <= dimensions <= 4096:
        raise ValueError(f"dimensions must be an integer from 1 to 4096, got {dimensions!r}")
    options = f"OPTIONS {{indexConfig: {{`vector.dimensions`: {dimensions}, `vector.similarity_function`: 'cosine'}}}}"
    return [
        f"CREATE VECTOR INDEX {TABLE_INDEX} IF NOT EXISTS FOR (n:Table) ON (n.{EMBEDDING_PROP}) {options}",
        f"CREATE VECTOR INDEX {COLUMN_INDEX} IF NOT EXISTS FOR (n:Column) ON (n.{EMBEDDING_PROP}) {options}",
    ]


def create_vector_indexes(dimensions: int, session: Any | None = None) -> None:
    """Create the table and column vector indexes (cosine). Safe to run repeatedly.

    An existing index keeps its original dimension: if you change embedding models,
    call `drop_vector_indexes` first, then re-run `embed_nodes` for every database.
    """
    statements = index_statements(dimensions)
    with use_session(session) as s:
        for statement in statements:
            s.run(statement).consume()


def drop_vector_indexes(session: Any | None = None) -> None:
    with use_session(session) as s:
        for name in (TABLE_INDEX, COLUMN_INDEX):
            s.run(f"DROP INDEX {name} IF EXISTS").consume()
