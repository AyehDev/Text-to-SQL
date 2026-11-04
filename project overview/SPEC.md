# SchemaGraph — Codebase Specification

This document defines how the codebase is organized, what each module exposes, the API endpoints, and the JSON formats passed between components. Its purpose is to let all four members build in parallel against agreed interfaces.

**Rule:** the data contracts in Section 3 (implemented in `schemagraph/models.py`) are shared. Any change to them goes through a pull request that the whole group reviews, because every component depends on them.

---

## 1. Repository layout

```
schemagraph/
├── docker-compose.yml          # PostgreSQL, Neo4j (+ GDS plugin), API
├── .env.example                # connection strings, API keys, model names
├── requirements.txt
├── SPEC.md                     # this file
│
├── contracts/
│   └── examples/               # sample JSON for every contract (used as mocks)
│       ├── metadata.adventureworks.sample.json
│       ├── join_plan.sample.json
│       └── ask_response.sample.json
│
├── schemagraph/                # Python package (backend)
│   ├── config.py               # settings loaded from .env          [shared]
│   ├── models.py               # Pydantic data contracts            [shared]
│   │
│   ├── db/                     # PostgreSQL access                   [Emmanuel]
│   │   └── postgres.py
│   │
│   ├── extraction/             # catalog extraction + inference      [Emmanuel]
│   │   ├── sql/
│   │   │   ├── tables.sql
│   │   │   ├── columns.sql
│   │   │   ├── primary_keys.sql
│   │   │   ├── foreign_keys.sql
│   │   │   └── unique_constraints.sql
│   │   ├── extract.py
│   │   └── infer.py
│   │
│   ├── graph/                  # Neo4j model, enrichment, planning   [Cameron]
│   │   ├── ingest.py
│   │   ├── weights.py
│   │   ├── enrich.py
│   │   ├── queries.py
│   │   ├── retrieve.py
│   │   └── planner.py
│   │
│   ├── pipeline/               # LangChain pipeline                  [Mohammed]
│   │   ├── context.py
│   │   ├── examples.py
│   │   ├── generate.py
│   │   ├── validate.py
│   │   ├── explain.py
│   │   └── pipeline.py
│   │
│   ├── api/                    # FastAPI app                          [Mohammed]
│   │   └── main.py
│   │
│   └── eval/                   # evaluation harness                   [Emmanuel]
│       ├── questions/
│       │   ├── adventureworks.jsonl
│       │   ├── bird_subset.jsonl
│       │   └── fewshot_examples.jsonl
│       ├── compare.py
│       ├── schema_metrics.py
│       └── run_eval.py
│
├── scripts/
│   ├── build_graph.py          # CLI: extract → infer → ingest → enrich
│   └── export_contracts.py     # writes JSON Schema from models.py
│
├── tests/                      # pytest, one file per module
│
└── frontend/                   # React + Vite                          [Davin]
    └── src/
        ├── api/
        │   ├── client.ts
        │   └── types.ts        # generated from the API's OpenAPI schema
        ├── components/
        └── mocks/              # copies of contracts/examples/*.json
```

**Ports:** PostgreSQL 5432 · Neo4j 7474 (browser) / 7687 (bolt) · API 8000 · frontend 5173.

---

## 2. Conventions

### 2.1 Identifiers

Several databases (AdventureWorks plus a few BIRD databases) share one Neo4j instance. Neo4j Community Edition supports only one user database, so every node carries a `db_id` and every identifier is prefixed with it.

| Thing | Format | Example |
|---|---|---|
| Database ID (`db_id`) | lowercase, no spaces | `adventureworks` |
| Table UID | `{db_id}:{schema}.{table}` | `adventureworks:sales.customer` |
| Column UID | `{db_id}:{schema}.{table}.{column}` | `adventureworks:sales.customer.customerid` |
| Display name (shown to users) | `{schema}.{table}[.{column}]` | `sales.customer.customerid` |

- Use names exactly as stored in the PostgreSQL catalog (PostgreSQL folds unquoted names to lowercase).
- Uniqueness constraints in Neo4j are on `uid`, never on bare names (bare names repeat across tables and schemas).

### 2.2 Relationship direction and cardinality

- A `REFERENCES` relationship always points **from the foreign-key column to the referenced column** (the "many" side to the "one" side).
- `cardinality` is `"N:1"`, or `"1:1"` when the foreign-key column is itself unique.
- Traversing a relationship in its stored direction is **many-to-one** (safe). Traversing against it is **one-to-many** (fan-out: can duplicate rows).

### 2.3 Modes

Every pipeline call takes a `mode`, which selects how schema context is built. All three share the same LLM, prompt template, few-shot examples, and validation loop.

| Mode | Schema context |
|---|---|
| `full` | DDL for every table in the database |
| `retrieval` | DDL for vector-search seed tables only, no joins |
| `graph` | Seed tables + bridge tables + explicit join conditions from the join plan |

### 2.4 General

- Python 3.11+, type hints everywhere, Pydantic v2 for all data passed between modules.
- Functions return Pydantic models, not raw dicts.
- LLM temperature is always 0. Model names come from `config.py`, never hardcoded.
- Every module has a pytest file. Pure functions (weights, result comparison, SQL table extraction) must have unit tests.

---

## 3. Data contracts (JSON formats)

All of these are defined as Pydantic models in `schemagraph/models.py`. `scripts/export_contracts.py` writes their JSON Schemas so the frontend and tests can validate against them.

### 3.1 Database metadata — extraction output, graph ingestion input

Produced by `extraction/extract.py` + `extraction/infer.py`. Consumed by `graph/ingest.py`. Saved as `data/metadata/{db_id}.json`.

```json
{
  "db_id": "adventureworks",
  "extracted_at": "2026-10-22T14:03:00Z",
  "schemas": [
    { "name": "sales" },
    { "name": "person" }
  ],
  "tables": [
    {
      "uid": "adventureworks:sales.customer",
      "schema": "sales",
      "name": "customer",
      "description": "Current customer information.",
      "description_source": "catalog",
      "row_count": 19820,
      "primary_key": ["customerid"]
    }
  ],
  "columns": [
    {
      "uid": "adventureworks:sales.customer.customerid",
      "table_uid": "adventureworks:sales.customer",
      "name": "customerid",
      "data_type": "integer",
      "ordinal_position": 1,
      "is_nullable": false,
      "is_primary_key": true,
      "is_unique": true,
      "description": "Primary key.",
      "description_source": "catalog",
      "sample_values": ["1", "2", "3", "4", "5"]
    }
  ],
  "relationships": [
    {
      "from_column": "adventureworks:sales.salesorderheader.customerid",
      "to_column": "adventureworks:sales.customer.customerid",
      "source": "declared",
      "confidence": 1.0,
      "cardinality": "N:1",
      "constraint_name": "fk_salesorderheader_customer_customerid",
      "evidence": null
    },
    {
      "from_column": "adventureworks:sales.store.salespersonid",
      "to_column": "adventureworks:sales.salesperson.businessentityid",
      "source": "inferred",
      "confidence": 0.72,
      "cardinality": "N:1",
      "constraint_name": null,
      "evidence": { "name_match": "suffix", "type_compatible": true, "value_overlap": 0.97 }
    }
  ]
}
```

| Field | Values / notes |
|---|---|
| `description_source` | `"catalog"` (from `obj_description`/`col_description`), `"generated"` (set later by enrichment), or `null` if none |
| `sample_values` | Up to 5 distinct non-null values as strings; used only for generating descriptions and inference |
| `source` | `"declared"` (from constraint views) or `"inferred"` |
| `confidence` | `1.0` for declared; 0–1 for inferred |
| `evidence` | `null` for declared; inference signals for inferred |

### 3.2 Seed tables — retrieval output

```json
[
  { "uid": "adventureworks:sales.salesorderheader", "score": 0.84, "matched_on": "column:totaldue" },
  { "uid": "adventureworks:person.person", "score": 0.79, "matched_on": "column:firstname" }
]
```

`matched_on` records whether the table matched on its own description (`"table"`) or through one of its columns (`"column:<name>"`). It is used in explanations.

### 3.3 Join plan — planner output

Produced by `graph/planner.py` (graph mode only). Consumed by `pipeline/context.py`, `pipeline/explain.py`, and evaluation.

```json
{
  "db_id": "adventureworks",
  "status": "full",
  "algorithm": "gds_steiner",
  "seed_tables": [
    { "uid": "adventureworks:sales.salesorderheader", "score": 0.84, "matched_on": "column:totaldue" },
    { "uid": "adventureworks:person.person", "score": 0.79, "matched_on": "column:firstname" }
  ],
  "bridge_tables": ["adventureworks:sales.customer"],
  "tables": [
    "adventureworks:sales.salesorderheader",
    "adventureworks:sales.customer",
    "adventureworks:person.person"
  ],
  "joins": [
    {
      "from_column": "adventureworks:sales.salesorderheader.customerid",
      "to_column": "adventureworks:sales.customer.customerid",
      "source": "declared",
      "confidence": 1.0,
      "cardinality": "N:1",
      "traversal": "many_to_one",
      "weight": 1.0
    },
    {
      "from_column": "adventureworks:sales.customer.personid",
      "to_column": "adventureworks:person.person.businessentityid",
      "source": "declared",
      "confidence": 1.0,
      "cardinality": "N:1",
      "traversal": "many_to_one",
      "weight": 1.0
    }
  ],
  "total_cost": 2.0,
  "unreachable_seeds": [],
  "dropped_seeds": [],
  "alternatives": [
    {
      "tables": [
        "adventureworks:sales.salesorderheader",
        "adventureworks:sales.salesperson",
        "adventureworks:humanresources.employee",
        "adventureworks:person.person"
      ],
      "total_cost": 3.0
    }
  ]
}
```

| Field | Values / notes |
|---|---|
| `status` | `"full"` (all seeds connected), `"partial"` (some seeds unreachable), `"single_table"` (one seed, no joins), `"none"` (no plan; pipeline falls back to retrieval context) |
| `algorithm` | `"gds_steiner"`, `"networkx_steiner"`, `"shortest_path"`, or `"none"` |
| `joins[].from_column` / `to_column` | Always in stored relationship direction (FK → referenced column), regardless of traversal |
| `traversal` | `"many_to_one"` or `"one_to_many"` (fan-out) |
| `dropped_seeds` | Seeds removed because the tree exceeded the cost/bridge threshold |
| `alternatives` | Optional runner-up paths, used to tell the user how an ambiguous question was interpreted |

### 3.4 Schema context — what goes into the prompt

Internal to the pipeline, built by `pipeline/context.py` for every mode.

```json
{
  "db_id": "adventureworks",
  "mode": "graph",
  "tables": ["adventureworks:sales.salesorderheader", "adventureworks:sales.customer", "adventureworks:person.person"],
  "ddl": "CREATE TABLE sales.salesorderheader (...);\nCREATE TABLE sales.customer (...);\n...",
  "join_text": "sales.salesorderheader.customerid = sales.customer.customerid\nsales.customer.personid = person.person.businessentityid",
  "join_plan": { "...": "JoinPlan or null" },
  "notes": []
}
```

`join_text` is empty and `join_plan` is `null` in `full` and `retrieval` modes. `notes` carries warnings to include in the prompt (e.g., a table with no known relationship).

### 3.5 API request and response — `POST /api/ask`

**Request**

```json
{
  "question": "What is the total order value for each customer's name?",
  "db_id": "adventureworks",
  "mode": "graph",
  "options": { "top_k": 5, "max_attempts": 3, "row_limit": 200 }
}
```

`mode` defaults to `"graph"`. `options` is optional; defaults come from `config.py`.

**Response**

```json
{
  "request_id": "6f1c2a0e-9b7d-4c1e-8a34-2d5e9f0b7c11",
  "question": "What is the total order value for each customer's name?",
  "db_id": "adventureworks",
  "mode": "graph",
  "status": "success",
  "sql": "SELECT p.firstname, p.lastname, SUM(soh.totaldue) AS total_order_value FROM sales.salesorderheader soh JOIN sales.customer c ON soh.customerid = c.customerid JOIN person.person p ON c.personid = p.businessentityid GROUP BY p.firstname, p.lastname;",
  "result": {
    "columns": ["firstname", "lastname", "total_order_value"],
    "rows": [["Jon", "Yang", "8248.99"], ["Eugene", "Huang", "6383.66"]],
    "row_count": 2,
    "truncated": false
  },
  "explanation": {
    "summary": "This adds up the total value of all orders for each customer and shows it next to the customer's name.",
    "tables": [
      { "uid": "adventureworks:sales.salesorderheader", "display_name": "sales.salesorderheader", "role": "seed", "reason": "Contains order totals (totaldue)." },
      { "uid": "adventureworks:sales.customer", "display_name": "sales.customer", "role": "bridge", "reason": "Needed to connect orders to people." },
      { "uid": "adventureworks:person.person", "display_name": "person.person", "role": "seed", "reason": "Contains first and last names." }
    ],
    "join_path": [
      { "from": "sales.salesorderheader.customerid", "to": "sales.customer.customerid", "source": "declared", "text": "Each order belongs to one customer." },
      { "from": "sales.customer.personid", "to": "person.person.businessentityid", "source": "declared", "text": "Each customer is linked to one person record." }
    ],
    "interpretation": "\"Person\" was interpreted as the customer, not the salesperson.",
    "warnings": []
  },
  "join_plan": { "...": "full JoinPlan from 3.3, or null outside graph mode" },
  "attempts": [
    { "sql": "SELECT ...", "error": null }
  ],
  "metrics": {
    "input_tokens": 1834,
    "output_tokens": 212,
    "attempt_count": 1,
    "latency_ms": { "retrieval": 120, "planning": 45, "generation": 2310, "validation": 18, "execution": 32, "explanation": 1400, "total": 3925 }
  }
}
```

| Field | Values / notes |
|---|---|
| `status` | `"success"`, `"invalid_sql"` (failed validation after all attempts), `"execution_error"`, `"no_schema"` (nothing relevant retrieved), `"unsafe_sql"` (not a single SELECT) |
| `sql` | Final SQL, or `null` if none was produced |
| `result.rows` | All values serialized as strings or `null` (avoids date/decimal JSON issues). Capped at `row_limit`; `truncated` is true if more rows existed |
| `explanation.tables[].role` | `"seed"`, `"bridge"`, or `"context"` (full/retrieval modes) |
| `attempts` | Every generated SQL, in order, with the database error if it failed |

**HTTP status codes:** `200` for every completed pipeline run, including failures reported in `status`, so the frontend can still show the explanation and attempts. `400` for bad input (empty question, unknown `db_id`, invalid mode). `503` if PostgreSQL, Neo4j, or the LLM is unreachable. Errors use FastAPI's default `{"detail": "..."}` body.

### 3.6 Evaluation question file — JSONL, one question per line

```json
{"id": "aw-017", "db_id": "adventureworks", "question": "What is the total order value for each customer's name?", "gold_sql": "SELECT ...", "tier": 3, "split": "test", "order_matters": false, "evidence": null, "author": "cameron", "notes": ""}
```

| Field | Values / notes |
|---|---|
| `tier` | `1` single table · `2` joins within one schema · `3` cross-schema joins of 3+ tables |
| `split` | `"test"` (the ~50 scored questions) or `"dev"` (~10 used only for tuning weights and prompts) |
| `order_matters` | `true` only if the question asks for an ordering (e.g., "top 10") |
| `evidence` | BIRD's external-knowledge hint, if any; `null` for AdventureWorks |

`fewshot_examples.jsonl` uses the same format but holds the example pairs shown to the LLM. **It must never contain test questions.**

### 3.7 Evaluation result file — JSONL, one row per question per mode

```json
{"question_id": "aw-017", "db_id": "adventureworks", "mode": "graph", "tier": 3, "correct": true, "status": "success", "generated_sql": "SELECT ...", "gold_tables": ["sales.salesorderheader", "sales.customer", "person.person"], "context_tables": ["sales.salesorderheader", "sales.customer", "person.person"], "schema_recall": 1.0, "bridge_tables_needed": ["sales.customer"], "bridge_tables_recovered": ["sales.customer"], "plan_status": "full", "input_tokens": 1834, "latency_ms": 3925, "attempt_count": 1, "llm_model": "<model>", "run_id": "2026-11-12-a", "timestamp": "2026-11-12T19:22:10Z"}
```

`bridge_tables_needed` = tables in the gold SQL that were not among the seed tables.

---

## 4. API endpoints

Defined in `schemagraph/api/main.py`. FastAPI generates OpenAPI docs at `/docs`; the frontend generates `types.ts` from `/openapi.json`.

| Method | Path | Purpose | Response |
|---|---|---|---|
| `GET` | `/api/health` | Check PostgreSQL, Neo4j, and LLM connectivity | `{"postgres": "ok", "neo4j": "ok", "llm": "ok"}` |
| `GET` | `/api/databases` | List databases available to query (for the frontend selector) | `[{"db_id": "adventureworks", "table_count": 68, "schema_count": 5}]` |
| `POST` | `/api/ask` | Main endpoint: question → SQL, result, explanation | `AskResponse` (3.5) |
| `POST` | `/api/plan` | Retrieval + join planning only, no LLM call. For debugging and for testing the graph independently | `{"seed_tables": [...], "join_plan": {...}}` |
| `GET` | `/api/schema/{db_id}/tables` | Browse tables (onboarding feature) | `[{"uid", "display_name", "description", "column_count"}]` |
| `GET` | `/api/schema/{db_id}/tables/{table_uid}` | One table with its columns and relationships | `{"table": {...}, "columns": [...], "references": [...], "referenced_by": [...]}` |

`/api/plan` takes the same request body as `/api/ask`. The two `/api/schema` endpoints are lower priority; build them after `/api/ask` works.

Graph building is **not** an endpoint. It runs from the command line with `scripts/build_graph.py`.

---

## 5. Modules and methods

Signatures are the contract; internal helper functions are up to each owner.

### 5.1 `config.py` — shared

```python
class Settings(BaseSettings):
    postgres_admin_dsn: str          # used only for loading data and extraction
    postgres_readonly_dsn: str       # template with {db_id}; used for all generated SQL
    neo4j_uri: str; neo4j_user: str; neo4j_password: str
    llm_model: str; embedding_model: str; embedding_dimensions: int
    top_k: int = 5
    max_attempts: int = 3
    row_limit: int = 200
    statement_timeout_ms: int = 10000
    max_bridge_tables: int = 4          # plans above this are treated as implausible
    weight_inferred_penalty: float = 5.0
    weight_fanout_penalty: float = 1.0

def get_settings() -> Settings
```

### 5.2 `db/postgres.py` — Emmanuel

```python
def list_databases() -> list[str]
def run_readonly_query(db_id: str, sql: str, row_limit: int) -> QueryResult
    # read-only role, statement_timeout set, rows serialized as strings
def explain(db_id: str, sql: str) -> ExplainResult        # ExplainResult(ok: bool, error: str | None)
def run_admin_query(db_id: str, sql: str, params: dict | None = None) -> list[dict]
    # extraction and inference only, never for LLM-generated SQL
```

### 5.3 `extraction/extract.py` — Emmanuel

```python
def extract_metadata(db_id: str) -> DatabaseMetadata
def fetch_tables(db_id: str) -> list[TableMeta]
def fetch_columns(db_id: str) -> list[ColumnMeta]
def fetch_primary_keys(db_id: str) -> dict[str, list[str]]      # table_uid -> column names
def fetch_foreign_keys(db_id: str) -> list[RelationshipMeta]     # source="declared"
def fetch_unique_columns(db_id: str) -> set[str]                 # column UIDs
def fetch_sample_values(db_id: str, column_uid: str, n: int = 5) -> list[str]
def save_metadata(meta: DatabaseMetadata, path: Path) -> None
def load_metadata(path: Path) -> DatabaseMetadata
```

SQL lives in `extraction/sql/*.sql` so it can be read and graded on its own.

### 5.4 `extraction/infer.py` — Emmanuel

```python
def find_candidate_pairs(meta: DatabaseMetadata) -> list[CandidatePair]
    # name matching + compatible types, skipping pairs already declared
def value_overlap(db_id: str, from_column: str, to_column: str) -> float
    # share of distinct from_column values that exist in to_column
def infer_relationships(db_id: str, meta: DatabaseMetadata,
                        min_confidence: float = 0.5) -> list[RelationshipMeta]
    # source="inferred", with confidence and evidence
```

### 5.5 `graph/ingest.py` — Cameron

```python
def create_constraints_and_indexes() -> None
    # UNIQUE on :Table(uid), :Column(uid); indexes on :Table(name), :Column(name), db_id
def clear_database(db_id: str) -> None
def ingest_metadata(meta: DatabaseMetadata) -> IngestReport
    # Schema/Table/Column nodes, CONTAINS, HAS_COLUMN, REFERENCES; idempotent (MERGE)
def apply_weights(db_id: str) -> None
    # sets weight_forward and weight_reverse on every REFERENCES edge
def verify_against_metadata(meta: DatabaseMetadata) -> list[str]
    # returns discrepancies (missing tables, etc.); empty list = OK
```

### 5.6 `graph/weights.py` — Cameron (pure functions, unit tested)

```python
def edge_weight(source: str, confidence: float, traversal: str, settings: Settings) -> float
    # 1 + inferred penalty * (1 - confidence) + fan-out penalty if traversal == "one_to_many"
```

### 5.7 `graph/enrich.py` — Cameron

```python
def generate_missing_descriptions(db_id: str) -> int
    # LLM writes descriptions from names + sample values; sets description_source="generated"
def embed_nodes(db_id: str, include_generated: bool = True) -> int
def create_vector_indexes() -> None
```

`include_generated=False` supports the ablation without generated descriptions.

### 5.8 `graph/queries.py` — Cameron

```python
def get_tables(db_id: str, uids: list[str]) -> list[TableDetail]   # table + columns, for DDL
def get_all_tables(db_id: str) -> list[TableDetail]                # full-schema mode
def list_tables(db_id: str) -> list[TableSummary]                  # /api/schema
def get_table_detail(db_id: str, uid: str) -> TableWithRelationships
```

### 5.9 `graph/retrieve.py` — Cameron

```python
def retrieve_seed_tables(db_id: str, question: str, k: int) -> list[SeedTable]
    # vector search over tables and columns; column hits roll up to their table (max score)
```

### 5.10 `graph/planner.py` — Cameron

```python
def plan_joins(db_id: str, seeds: list[SeedTable],
               include_inferred: bool = True, weighted: bool = True) -> JoinPlan
```

Internally: group seeds by connected component → Steiner tree (GDS, NetworkX fallback) → check `max_bridge_tables` → map table links back to column pairs → find runner-up path for `alternatives`. `include_inferred=False` and `weighted=False` support the ablations. **Never raises for "no path"**; returns `status="partial"` or `"none"` instead.

### 5.11 `pipeline/context.py` — Mohammed

```python
def build_context(db_id: str, question: str, mode: Mode, top_k: int) -> tuple[SchemaContext, list[SeedTable]]
def render_ddl(tables: list[TableDetail]) -> str      # CREATE TABLE text with PK/FK and comments
def render_joins(plan: JoinPlan) -> str
```

In `graph` mode, if `plan.status == "none"`, fall back to retrieval context and add a note.

### 5.12 `pipeline/examples.py` — Mohammed

```python
def retrieve_examples(db_id: str, question: str, k: int = 3) -> list[FewShotExample]
    # from fewshot_examples.jsonl only
```

### 5.13 `pipeline/generate.py` — Mohammed

```python
def build_prompt(question: str, context: SchemaContext, examples: list[FewShotExample],
                 previous_attempts: list[Attempt]) -> list[BaseMessage]
def generate_sql(question: str, context: SchemaContext, examples: list[FewShotExample],
                 previous_attempts: list[Attempt]) -> GenerationResult
    # GenerationResult(sql, raw_output, input_tokens, output_tokens)
def extract_sql(llm_output: str) -> str | None
```

The prompt template is identical across modes; only the context sections differ.

### 5.14 `pipeline/validate.py` — Mohammed

```python
def is_single_select(sql: str) -> bool        # sqlglot parse; rejects writes and multiple statements
def validate(db_id: str, sql: str) -> ExplainResult
```

### 5.15 `pipeline/explain.py` — Mohammed

```python
def build_explanation(question: str, sql: str, context: SchemaContext,
                      seeds: list[SeedTable]) -> Explanation
```

Tables and join path are built from the join plan with templates (no LLM needed). Only `summary` uses the LLM. `interpretation` is filled when `alternatives` is non-empty.

### 5.16 `pipeline/pipeline.py` — Mohammed

```python
def answer_question(request: AskRequest, explain: bool = True) -> AskResponse
```

The single entry point. **Both the API and the evaluation harness call this function**, so evaluation measures exactly what users get. `explain=False` lets evaluation skip the explanation's LLM call.

Flow: `build_context` → `retrieve_examples` → loop up to `max_attempts` { `generate_sql` → `is_single_select` → `validate` } → `run_readonly_query` → `build_explanation` → assemble `AskResponse` with timings.

### 5.17 `api/main.py` — Mohammed

Thin routes only: validate input, call `answer_question` / planner / queries, map exceptions to HTTP codes. CORS enabled for `localhost:5173`.

### 5.18 `eval/compare.py` — Emmanuel (unit tested)

```python
def results_match(gold: QueryResult, predicted: QueryResult, order_matters: bool) -> bool
    # compares as multisets of rows unless order matters; normalizes numbers and NULLs
```

### 5.19 `eval/schema_metrics.py` — Emmanuel (unit tested)

```python
def tables_in_sql(sql: str, db_id: str) -> set[str]                 # sqlglot, postgres dialect
def schema_recall(gold_tables: set[str], context_tables: set[str]) -> float
def bridge_tables_needed(gold_tables: set[str], seed_tables: set[str]) -> set[str]
```

### 5.20 `eval/run_eval.py` — Emmanuel

```python
def load_questions(path: Path, split: str = "test") -> list[Question]
def run_eval(question_files: list[Path], modes: list[Mode], out_path: Path,
             run_id: str, planner_options: dict | None = None) -> None
def summarize(results_path: Path) -> pd.DataFrame     # accuracy etc. by mode × tier
```

Gold SQL is executed with `run_readonly_query` and cached, so each gold query runs once per run.

### 5.21 `scripts/build_graph.py`

```
python scripts/build_graph.py --db adventureworks [--skip-inference] [--skip-enrichment]
```

Runs `extract_metadata` → `infer_relationships` → `save_metadata` → `ingest_metadata` → `apply_weights` → `generate_missing_descriptions` → `embed_nodes` → `verify_against_metadata`.

### 5.22 Frontend — Davin

**`src/api/client.ts`**

```ts
getHealth(): Promise<Health>
getDatabases(): Promise<DatabaseInfo[]>
ask(req: AskRequest): Promise<AskResponse>
plan(req: AskRequest): Promise<PlanResponse>
listTables(dbId: string): Promise<TableSummary[]>
getTable(dbId: string, uid: string): Promise<TableWithRelationships>
```

**Components:** `DatabaseSelector`, `QuestionInput`, `ResultTable`, `SqlView`, `TablesUsed` (seed vs. bridge badges), `JoinPathView`, `ExplanationPanel`, `WarningsView`, `AttemptsView` (collapsed by default), and a `ModeSelector` shown only in a developer view.

Build against `src/mocks/ask_response.json` until the backend is ready, with a `VITE_USE_MOCKS=true` switch in `client.ts`.

---

## 6. Working in parallel

### 6.1 What each person can build before anyone else is finished

| Owner | Starts from |
|---|---|
| Emmanuel | PostgreSQL alone. Writes extraction and the eval harness (gold queries run without the pipeline). |
| Cameron | `contracts/examples/metadata.adventureworks.sample.json` (a small hand-written file covering ~6 tables) until real extraction output exists. |
| Mohammed | A stub `retrieve_seed_tables` returning fixed tables and a stub `plan_joins` returning `join_plan.sample.json`. Full-schema mode needs only `get_all_tables`, which can also be stubbed from the sample metadata. |
| Davin | `ask_response.sample.json` and the mock switch. |

### 6.2 Integration milestones

1. **End of week 1:** full-schema mode answers a question end to end through the API and frontend (stubs allowed elsewhere).
2. **End of week 2:** real extraction → real graph → retrieval mode working.
3. **End of week 3:** graph mode working; `run_eval.py` produces a results file for all three modes.

### 6.3 Repository rules

- `main` always runs. Work on branches; merge through pull requests with one reviewer.
- Changes to `models.py`, `config.py`, or `contracts/` need review from everyone affected.
- Every new contract change updates the matching sample in `contracts/examples/`.
- Never commit `.env` or API keys.

---

## 7. Safety requirements

- All LLM-generated SQL runs through `run_readonly_query`, using a PostgreSQL role with `SELECT` only.
- `statement_timeout` is set on every read-only connection.
- `is_single_select` must pass before `explain` is even called.
- The admin connection is used only by extraction, inference, and data loading.
