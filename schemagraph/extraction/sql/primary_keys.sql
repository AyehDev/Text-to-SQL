-- primary_keys.sql: one row per primary-key column, in key order.
--
-- key_position keeps the order inside composite keys (junction tables such as
-- person.businessentityaddress have three-column keys), so extract.py can fill
-- TableMeta.primary_key in the right order.
--
-- unnest(...) WITH ORDINALITY is what preserves that order: conkey is an array
-- of column numbers in key order, and joining pg_attribute directly would lose it.

SELECT
    n.nspname   AS schema_name,
    c.relname   AS table_name,
    a.attname   AS column_name,
    k.ord::int  AS key_position
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE con.contype = 'p'
  AND c.relkind IN ('r', 'p')
  AND NOT c.relispartition
  AND n.nspname !~ '^pg_'
  AND n.nspname <> 'information_schema'
  AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_class'::regclass
          AND d.objid = c.oid
          AND d.deptype = 'e')
ORDER BY n.nspname, c.relname, k.ord;
