-- tables.sql: one row per base table (ordinary 'r' or partitioned parent 'p').
--
-- Which tables count (the same filter is repeated in every file in this folder,
-- so all five queries agree on what exists):
--   * views, materialized views, sequences, foreign tables: skipped
--   * system schemas (pg_*, information_schema): skipped
--   * partitions of a partitioned table: skipped; the parent stands for them
--   * tables owned by an extension (e.g. PostGIS spatial_ref_sys): skipped
--
-- row_estimate comes from the planner statistics, which is free but may be
-- stale. It is NULL when the table was never analyzed (-1 on PostgreSQL 14+).
-- extract.py falls back to an exact COUNT(*) when it is NULL or 0.

SELECT
    n.nspname                                  AS schema_name,
    c.relname                                  AS table_name,
    obj_description(c.oid, 'pg_class')         AS description,
    CASE WHEN c.reltuples >= 0 THEN c.reltuples::bigint END AS row_estimate
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND NOT c.relispartition
  AND n.nspname !~ '^pg_'
  AND n.nspname <> 'information_schema'
  AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_class'::regclass
          AND d.objid = c.oid
          AND d.deptype = 'e')
ORDER BY n.nspname, c.relname;
