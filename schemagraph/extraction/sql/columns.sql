-- columns.sql: one row per column of every table that tables.sql returns.
--
-- data_type uses format_type(), which prints the type the way DDL would
-- ("integer", "character varying(50)", "numeric(10,2)"), not the internal name.
--
-- ordinal_position is renumbered 1..n in column order. The raw attnum has gaps
-- wherever a column was dropped, which would only confuse readers of the JSON.
-- Dropped columns and system columns (attnum <= 0) are skipped.

SELECT
    n.nspname                                              AS schema_name,
    c.relname                                              AS table_name,
    a.attname                                              AS column_name,
    format_type(a.atttypid, a.atttypmod)                   AS data_type,
    row_number() OVER (PARTITION BY c.oid ORDER BY a.attnum)::int AS ordinal_position,
    NOT a.attnotnull                                       AS is_nullable,
    col_description(c.oid, a.attnum)                       AS description
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p')
  AND NOT c.relispartition
  AND n.nspname !~ '^pg_'
  AND n.nspname <> 'information_schema'
  AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_class'::regclass
          AND d.objid = c.oid
          AND d.deptype = 'e')
ORDER BY n.nspname, c.relname, a.attnum;
