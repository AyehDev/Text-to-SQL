-- unique_constraints.sql: every column set that is guaranteed unique.
--
-- One row per column per unique index, grouped by index_name. This covers
-- primary keys, UNIQUE constraints and plain unique indexes, because
-- PostgreSQL implements all three as a unique index.
--
-- extract.py uses it twice:
--   * a group with ONE column  -> that column is_unique (unique on its own)
--   * a group matching ALL the columns of a foreign key -> that FK is 1:1
-- (A column inside a composite key is NOT unique by itself, so it stays false.)
--
-- What is deliberately left out, because it does not guarantee uniqueness of
-- the whole table:
--   * partial unique indexes (WHERE ...): only unique among some rows
--   * expression indexes (e.g. lower(email)): not a plain column
--   * invalid indexes (a failed CREATE INDEX CONCURRENTLY)
-- INCLUDE columns are not part of the key, so they are ignored too.

SELECT
    n.nspname       AS schema_name,
    c.relname       AS table_name,
    ic.relname      AS index_name,
    i.indisprimary  AS is_primary,
    a.attname       AS column_name,
    k.ord::int      AS key_position
FROM pg_index i
JOIN pg_class c      ON c.oid = i.indrelid
JOIN pg_class ic     ON ic.oid = i.indexrelid
JOIN pg_namespace n  ON n.oid = c.relnamespace
CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a  ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE i.indisunique
  AND i.indisvalid
  AND i.indpred IS NULL
  AND i.indexprs IS NULL
  AND k.ord <= i.indnkeyatts
  AND c.relkind IN ('r', 'p')
  AND NOT c.relispartition
  AND n.nspname !~ '^pg_'
  AND n.nspname <> 'information_schema'
  AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_class'::regclass
          AND d.objid = c.oid
          AND d.deptype = 'e')
ORDER BY n.nspname, c.relname, ic.relname, k.ord;
