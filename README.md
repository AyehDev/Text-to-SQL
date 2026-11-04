# SchemaGraph

Text-to-SQL for PostgreSQL. A question goes in; a checked, read-only SQL query and its result come out.

```
Postgres ──extract/infer──▶ metadata JSON ──ingest──▶ Neo4j graph ──▶ retrieve ──▶ join planner ──▶ LLM ──▶ SQL ──▶ result
 (catalog)    (Emmanuel)     (the contract)   (Cameron)   (tables,       (tables?)    (join path)   (Mohammed)       (read-only role)
                                                          columns,
                                                          joins)
```

The metadata JSON is the hand-off between the extraction side and the graph side. Everything
downstream reads that file's shape (`schemagraph/models.py`), so changes to it need everyone's review.

## Who owns what

| Person | Slice | Where | Status |
| --- | --- | --- | --- |
| **Emmanuel** | Postgres access, metadata extraction, relationship inference, evaluation harness | `schemagraph/db/`, `schemagraph/extraction/`, `schemagraph/eval/`, `scripts/` | Postgres access, extraction and inference built and tested. Evaluation harness (`compare.py`, `schema_metrics.py`, `run_eval.py`) not started; only `infer_recovery.py` exists |
| **Cameron** | Graph: weights, ingest, retrieval, join planner, graph queries | `schemagraph/graph/` | `weights`, `driver`, `ingest` written; Cypher needs a first run on a real Neo4j. Retrieval, planner and queries not started |
| **Mohammed** | LLM pipeline and FastAPI | `schemagraph/pipeline/`, `schemagraph/api/` | Not started |
| **Davin** | React frontend | `frontend/` | Not started |

The full design is in `SPEC.md`. Section numbers quoted in the code (3 for contracts, 5.6 for edge
weights, 7 for the two Postgres roles) refer to it.

### If you are Cameron (graph)

- **Input:** a `DatabaseMetadata` file. Start from `contracts/examples/metadata.adventureworks.sample.json`
  (7 tables, 93 columns, 5 relationships, one of them inferred). No database is needed to work with it.
- **Already there:** `graph/weights.py` (edge cost, SPEC 5.6), `graph/driver.py` (one shared Neo4j driver),
  `graph/ingest.py` (constraints, ingest, `apply_weights`, `verify_against_metadata`, `clear_database`).
- **Graph shape:** `(:Schema)-[:CONTAINS]->(:Table)-[:HAS_COLUMN]->(:Column)` and
  `(:Column)-[:REFERENCES]->(:Column)`, always from the FK column to the referenced column. Every
  `REFERENCES` edge carries `source`, `confidence`, `cardinality`, `weight_forward`, `weight_reverse`,
  `evidence_json`, `from_table` and `to_table`.
- **Decisions to confirm:** Schema uid is `{db_id}:{schema}`; a 1:1 edge costs the same in both directions;
  inferred confidence is capped at 0.95, so an inferred edge always costs more than a declared one.
- **First thing to do:** run `tests/test_ingest_neo4j.py` against your Neo4j (see Testing). The Cypher has only
  been tested against a fake session so far.
- **Weights:** forward = following the arrow (many-to-one), reverse = against it (one-to-many, can multiply rows).
  Declared forward is 1.0, declared reverse is 2.0. Note the signature differs from SPEC 5.6:
  `edge_weight(confidence, "forward" | "reverse")` and `edge_weights(confidence, cardinality)` instead of
  `edge_weight(source, confidence, traversal, settings)`. The planner's `traversal` values
  `"many_to_one"` / `"one_to_many"` map to `"forward"` / `"reverse"`. Agree which form you want and change it once.
- **Still to build:** `enrich.py` (descriptions, embeddings, vector indexes), `queries.py`, `retrieve.py`, `planner.py`
  (SPEC 5.7 to 5.10), and `scripts/build_graph.py`. `build_metadata` already covers its first three steps
  (extract, infer, save); the rest of its flow is ingest, weights, enrichment, embeddings, verify.

### If you are Mohammed (pipeline and API)

- **Read-only SQL:** everything the model writes must run through `schemagraph.db.postgres.run_readonly_query`.
  It uses the `sg_readonly` login, a statement timeout, and read-only transactions, and returns at most
  `row_limit` rows with a truncation flag. `explain()` is there for checking a query without running it.
  Do not open your own Postgres connection for LLM SQL.
- **Databases:** `list_databases()` gives what the read-only role can connect to. You will probably want to
  intersect it with the databases that have a graph.
- **Contracts:** the result types (`GenerationResult`, `ExplainResult`, `TableDetail`, `TableSummary`, ...) are in
  `schemagraph/models.py`. Dump with `by_alias=True`: two fields are aliased because their names clash with
  Python (`schema`, `from`).
- **Config:** add your section to `config.py` (`llm`, `pipeline`) the way `neo4j` was added: a Pydantic model,
  an optional field on `AppConfig`, secrets by environment variable name. Never put a key in `config.yml`.

### If you are Davin (frontend)

- **API shapes** come from the Pydantic models in `schemagraph/models.py`; those are the JSON your app
  receives. `contracts/examples/` holds real example data, and more will be added there.
- **Name display:** UIDs look like `db:schema.table.column`. Show `models.display_name(uid)`, and never split a
  UID yourself, because table and column names can contain dots.
- **Inferred relationships** have `source: "inferred"`, a `confidence` below 1.0 and an `evidence` object
  (`name_match`, `type_compatible`, `value_overlap`). Declared ones have `confidence: 1.0` and no evidence.
  The UI should make the difference visible.

### If you are Emmanuel (extraction)

Details are in "What each folder does" below. Open items: the question-answering evaluation harness
(SPEC 5.18 to 5.20: `results_match`, schema metrics, `run_eval.py`), the held-out inference test on BIRD,
`.env` loading for passwords, and the database-wrapper abstraction (deferred on purpose).

## Setup

Requirements: Python 3.11+, Docker, and `psql`/`pg_dump` version 15 or newer (`create_roles.sql` uses `\getenv`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Databases

`docker-compose.yml` runs Postgres 17 and Neo4j 5 (community), both bound to `127.0.0.1` only.
Put the passwords in a `.env` file next to it (git-ignored; never commit it):

```
DB_PASSWORD=...          # Postgres superuser "app"
APP_DB_PASSWORD=...
NEO4J_PASSWORD=...       # at least 8 characters; read once, when the volume is first created
```

```bash
docker compose up -d
```

### Configuration

```bash
cp config.example.yml config.yml     # not committed
export SCHEMAGRAPH_CONFIG=$PWD/config.yml
export PG_ADMIN_PASSWORD=...  PG_READONLY_PASSWORD=...  NEO4J_PASSWORD=...
```

`config.yml` names environment variables for each password (`password_env`); the passwords themselves
never go in the file. The two Postgres logins exist for a reason:

- `sg_admin` reads the catalog and samples data (extraction, inference, loading). It never runs LLM SQL.
- `sg_readonly` has SELECT only, plus a timeout and read-only transactions. It runs every LLM-generated query,
  so a bad or malicious query cannot change data or schema. The database enforces this, not a check on the SQL text.

### Data sources

There are two databases. Neither is stored in this repository; each load script fetches or reads them.

| Source | What it is | Where to get it | Used for |
| --- | --- | --- | --- |
| **AdventureWorks** | Microsoft's sample company database: 68 tables in 5 schemas (`humanresources`, `person`, `production`, `purchasing`, `sales`). Nearly every foreign key is declared. | Two parts. (1) The Postgres port: clone <https://github.com/lorint/AdventureWorks-for-Postgres>. (2) Microsoft's CSV data, which `load_adventureworks.sh` downloads for you from <https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorks-oltp-install-script.zip> (about 17 MB). | The main evaluation database: cross-schema joins, hand-written questions (about 60, split 50 test / 10 dev). |
| **BIRD mini-dev** | A public Text-to-SQL benchmark: 500 questions over about a dozen small databases, each with an `evidence` hint. Ships with a PostgreSQL dump and PostgreSQL-flavoured gold queries. | The `bird-bench/mini_dev` git repo (<https://github.com/bird-bench/mini_dev>) holds code only, not the data. The data is a separate download, linked from its README: the Aliyun zip <https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip> (the server is in Beijing and often stalls from outside China), or the "complete package" on Google Drive (<https://drive.google.com/file/d/13VLWIwpw5E3d5DUkMvzw7hvHE67a4XkG/view>). The question files are also on Hugging Face (<https://huggingface.co/datasets/birdsql/bird_mini_dev>, split `mini_dev_pg`); I have not confirmed that it includes the database dump. | A held-out test for relationship inference and for evaluation on databases the rules were not tuned on. |

Inside the BIRD zip, the files we use are `MINIDEV_postgresql/BIRD_dev.sql` (the dump) and
`mini_dev_postgresql.json` (the questions). BIRD's own instructions load the whole dump into one database
called `BIRD`; SchemaGraph needs one database per `db_id`, which `load_bird.sh` arranges. Check each dataset's
licence terms before redistributing it or committing any of it to the repo.

### Load sample data

```bash
export PGHOST=127.0.0.1 PGPORT=5432 PGUSER=app PGPASSWORD=$DB_PASSWORD   # a superuser

# AdventureWorks. Needs git, Ruby (to convert the CSVs once) and a clone of the lorint repo:
git clone https://github.com/lorint/AdventureWorks-for-Postgres adventureworks-for-postgres
scripts/load_adventureworks.sh --repo ./adventureworks-for-postgres

# BIRD mini-dev. Download minidev.zip first (table above), then:
scripts/load_bird.sh --zip ~/Downloads/minidev.zip
# if the dump layout is not recognised the script stops and prints the table list; send that to Emmanuel
```

Both scripts create lowercase UTF8 databases, run `scripts/create_roles.sql`, run `ANALYZE` and check that
`sg_readonly` can read but not write. A database loaded any other way needs
`psql -d <db> -f scripts/create_roles.sql` as a superuser, or `sg_admin` gets "permission denied" on its tables.

### Build the metadata and the graph

```bash
python -m schemagraph.extraction.build_metadata --db adventureworks     # writes data/metadata/adventureworks.json
python -m schemagraph.graph.ingest --db adventureworks --rebuild        # loads it into Neo4j and verifies
```

`build_metadata` accepts `--all`, repeated `--db`, and `--skip-inference`.

## API reference

The backend is a FastAPI app (`schemagraph/api/main.py`, Mohammed). It serves OpenAPI docs at `/docs` and the
schema at `/openapi.json`; the frontend generates `types.ts` from that. CORS allows `http://localhost:5173`.
`SPEC.md` section 3 and 4 is the source of truth; this is the short version. Where the two differ, fix this file.

### Endpoints

| Method | Path | Purpose | Response |
| --- | --- | --- | --- |
| `GET` | `/api/health` | Checks PostgreSQL, Neo4j and the LLM | `{"postgres": "ok", "neo4j": "ok", "llm": "ok"}` |
| `GET` | `/api/databases` | Databases available to query (the selector) | `[{"db_id": "adventureworks", "table_count": 68, "schema_count": 5}]` |
| `POST` | `/api/ask` | Main endpoint: question to SQL, result and explanation | `AskResponse` |
| `POST` | `/api/plan` | Retrieval and join planning only, no LLM call. Same body as `/api/ask`. For debugging the graph on its own | `{"seed_tables": [...], "join_plan": {...}}` |
| `GET` | `/api/schema/{db_id}/tables` | Browse tables (lower priority; build after `/api/ask`) | `[{"uid", "display_name", "description", "column_count"}]` |
| `GET` | `/api/schema/{db_id}/tables/{table_uid}` | One table with columns and relationships (lower priority) | `{"table", "columns", "references", "referenced_by"}` |

Building the graph is not an endpoint. It runs from the command line.

### `POST /api/ask`

Request:

```json
{
  "question": "What is the total order value for each customer's name?",
  "db_id": "adventureworks",
  "mode": "graph",
  "options": { "top_k": 5, "max_attempts": 3, "row_limit": 200 }
}
```

`mode` is one of `full` (whole schema in the prompt), `retrieval` (top-k tables), or `graph` (retrieval plus a
join plan); it defaults to `graph`. `options` is optional and defaults come from `config.yml`.

Response (abridged):

```json
{
  "request_id": "6f1c2a0e-...",
  "question": "...", "db_id": "adventureworks", "mode": "graph",
  "status": "success",
  "sql": "SELECT p.firstname, p.lastname, SUM(soh.totaldue) ... GROUP BY p.firstname, p.lastname;",
  "result": { "columns": ["firstname", "lastname", "total_order_value"],
             "rows": [["Jon", "Yang", "8248.99"]], "row_count": 1, "truncated": false },
  "explanation": {
    "summary": "This adds up the total value of all orders for each customer...",
    "tables": [{ "uid": "adventureworks:sales.customer", "display_name": "sales.customer",
                 "role": "bridge", "reason": "Needed to connect orders to people." }],
    "join_path": [{ "from": "sales.salesorderheader.customerid", "to": "sales.customer.customerid",
                    "source": "declared", "text": "Each order belongs to one customer." }],
    "interpretation": "\"Person\" was interpreted as the customer, not the salesperson.",
    "warnings": []
  },
  "join_plan": { "...": "JoinPlan, or null outside graph mode" },
  "attempts": [{ "sql": "SELECT ...", "error": null }],
  "metrics": { "input_tokens": 1834, "output_tokens": 212, "attempt_count": 1,
              "latency_ms": { "retrieval": 120, "planning": 45, "generation": 2310, "validation": 18,
                              "execution": 32, "explanation": 1400, "total": 3925 } }
}
```

| Field | Values and notes |
| --- | --- |
| `status` | `success`; `invalid_sql` (failed validation after all attempts); `execution_error`; `no_schema` (nothing relevant retrieved); `unsafe_sql` (not a single SELECT) |
| `sql` | The final SQL, or `null` if none was produced |
| `result.rows` | Every value is a string or `null`, which avoids date and decimal problems in JSON. Capped at `row_limit`; `truncated` is true if more rows existed |
| `explanation.tables[].role` | `seed` (matched the question), `bridge` (needed to connect seeds), or `context` (full and retrieval modes) |
| `explanation.join_path[].source` | `declared` or `inferred`. Show the difference to the user |
| `attempts` | Every generated SQL in order, with the database error if it failed. Collapsed by default in the UI |

HTTP codes:

- `200` for every completed run, including failures reported in `status`, so the UI can still show the explanation and attempts.
- `400` for bad input: empty question, unknown `db_id`, invalid mode.
- `503` if PostgreSQL, Neo4j or the LLM is unreachable.
- Errors use FastAPI's default body, `{"detail": "..."}`.

### Join plan (`/api/plan`, and `join_plan` inside `/api/ask`)

`status` is `full` (all seeds connected), `partial` (some unreachable), `single_table` (one seed, no joins) or
`none` (no plan; the pipeline falls back to retrieval context). `algorithm` is `gds_steiner`,
`networkx_steiner`, `shortest_path` or `none`. Each entry in `joins` has `from_column`, `to_column` (always the
stored direction, FK column to referenced column, whichever way the plan walks it), `source`, `confidence`,
`cardinality` (`N:1` or `1:1`), `traversal` (`many_to_one` or `one_to_many`) and `weight`. The plan also lists
`seed_tables`, `bridge_tables`, `tables`, `total_cost`, `unreachable_seeds`, `dropped_seeds` and optional
`alternatives` (runner-up paths used to explain an ambiguous question). The planner never raises for "no path";
it returns `partial` or `none`.

### Internal interfaces each owner implements

Signatures are the contract; helpers are up to the owner. All are in SPEC section 5.

| Module | Owner | Main functions |
| --- | --- | --- |
| `db/postgres.py` | Emmanuel (done) | `list_databases()`, `run_readonly_query(db_id, sql, row_limit)`, `explain(db_id, sql)`, `run_admin_query(db_id, sql, params)` |
| `extraction/extract.py`, `infer.py` | Emmanuel (done) | `extract_metadata(db_id)`, `save_metadata`, `load_metadata`, `infer_relationships(db_id, meta)` |
| `eval/compare.py`, `schema_metrics.py`, `run_eval.py` | Emmanuel | `results_match(gold, predicted, order_matters)`, `schema_recall`, `bridge_tables_needed`, `run_eval(...)`, `summarize(...)` |
| `graph/ingest.py`, `weights.py` | Cameron (written) | `ingest_metadata`, `apply_weights`, `verify_against_metadata`, `clear_database`, `edge_weight` |
| `graph/enrich.py`, `queries.py`, `retrieve.py`, `planner.py` | Cameron | `embed_nodes`, `get_tables`, `list_tables`, `get_table_detail`, `retrieve_seed_tables(db_id, question, k)`, `plan_joins(db_id, seeds, include_inferred, weighted)` |
| `pipeline/context.py`, `examples.py`, `generate.py`, `validate.py`, `explain.py` | Mohammed | `build_context`, `retrieve_examples`, `generate_sql`, `is_single_select`, `validate`, `build_explanation` |
| `pipeline/pipeline.py` | Mohammed | `answer_question(request, explain=True)`: the single entry point |
| `api/main.py` | Mohammed | Thin routes: validate input, call the above, map errors to HTTP codes |
| `src/api/client.ts` | Davin | `getHealth`, `getDatabases`, `ask`, `plan`, `listTables`, `getTable` |

Two rules hold the pieces together:

1. **One entry point.** Both the API and the evaluation harness call `answer_question`, so evaluation measures
   exactly what users get.
2. **One way to run model SQL.** `is_single_select` (a parse that rejects writes and multiple statements) must
   pass, then the query runs through `run_readonly_query`. Nothing else executes generated SQL.

### File formats

- **Question files** (evaluation): JSONL, one question per line:
  `{"id": "aw-017", "db_id": "adventureworks", "question": "...", "gold_sql": "SELECT ...", "tier": 3, "split": "test", "order_matters": false, "evidence": null, "author": "cameron", "notes": ""}`.
  `tier` is 1 (single table), 2 (joins within a schema) or 3 (cross-schema, 3+ tables). `split` is `test` (about 50
  scored questions) or `dev` (about 10, only for tuning). `evidence` is BIRD's hint, `null` for AdventureWorks.
  `fewshot_examples.jsonl` has the same format and **must never contain test questions**.
- **Result files**: JSONL, one row per question per mode, with `correct`, `status`, `generated_sql`, `context_tables`,
  `schema_recall`, `bridge_tables_needed`, `bridge_tables_recovered`, `plan_status`, token and latency fields, and a `run_id`.
- **Examples to build against:** `contracts/examples/metadata.adventureworks.sample.json` exists now. The plan
  also calls for `join_plan.sample.json` and `ask_response.sample.json` (the frontend mock, switched with
  `VITE_USE_MOCKS=true`); they are not written yet.

## What each folder does

| Path | Purpose |
| --- | --- |
| `schemagraph/models.py` | Every shared data contract, plus the UID helpers. Change with care. |
| `schemagraph/config.py` | YAML config, the two Postgres roles, connection strings, Neo4j settings. |
| `schemagraph/db/postgres.py` | The only code that talks to Postgres: read-only queries, `explain`, admin queries. |
| `schemagraph/extraction/sql/*.sql` | Catalog queries: tables, columns, primary keys, foreign keys, unique constraints. |
| `schemagraph/extraction/extract.py` | Turns those results into `DatabaseMetadata`; saves and loads the JSON. |
| `schemagraph/extraction/infer.py` | Finds relationships the database does not declare: name and type fit, confirmed by value overlap. |
| `schemagraph/extraction/build_metadata.py` | One command: extract, infer, save. |
| `schemagraph/eval/infer_recovery.py` | Hides declared foreign keys and measures how many inference recovers. |
| `schemagraph/graph/` | Weights, Neo4j driver, ingest. Cameron adds retrieval, planner and queries here. |
| `scripts/` | Role creation and the two data loaders. |
| `contracts/examples/` | Example data files that teammates can build against. |
| `tests/` | Everything below. |

## Testing

```bash
pytest -q                                   # no databases needed
```

Tests that need a database are skipped unless you opt in:

| Switch | Enables |
| --- | --- |
| `SG_TEST_PG_DSN="host=127.0.0.1 port=5432 user=app dbname=postgres"` | Integration tests on throwaway Postgres databases. The user must be able to create databases. |
| `SG_TEST_ADVENTUREWORKS=1` | Checks against the real AdventureWorks database (also needs the config and passwords above). |
| `SG_TEST_NEO4J=1` | Graph tests against Neo4j. They use a throwaway `sgtest_<random>` db_id and delete it afterwards. |

Before you push, run once with everything you have available. A skipped test has not passed.

## Conventions

- **UIDs** are `{db_id}:{schema}.{table}[.{column}]`. Build them with the helpers in `models.py`; never parse them.
  Use `ColumnMeta.table_uid` to go from a column to its table.
- **`db_id`** is lowercase letters, digits and underscores, and equals the Postgres database name.
- **Dump models with `by_alias=True`.**
- **Secrets** come from environment variables. `config.yml` and `.env` are never committed.
- **Pure code first.** Decide-what-to-do logic is plain functions with tests that need no database;
  the database-touching functions stay thin. New modules follow that pattern.
- **Tests that break things on purpose:** when you add a rule, break it deliberately and confirm a test fails.
  A test that passes either way proves nothing.
- **Changing shared files** (`models.py`, `config.py`): tell the others first.

## Known limits

- Relationship inference was tuned on AdventureWorks. Its accuracy on other databases is not measured yet; BIRD is the planned test.
- Inference proposes single-column relationships only. Declared composite keys are extracted correctly.
- Two tables with identical key sets cannot be told apart by the data, so inference picks one deterministically.
- The Postgres wrapper is Postgres-only by choice; abstracting it for other engines was deferred.