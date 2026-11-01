-- foreign_keys.sql: one row per COLUMN PAIR of every declared foreign key.
--
-- A composite FK (two or more columns) comes back as several rows that share a
-- constraint_name; key_position says which pair is which. extract.py turns each
-- row into one RelationshipMeta (the model is column-to-column).
--
-- unnest(conkey, confkey) walks the referencing and referenced column numbers
-- in lockstep, so column 1 is paired with column 1 and so on. Pairing them any
-- other way (information_schema.constraint_column_usage, for example) can match
-- the wrong columns on composite keys.
--
-- Both ends must be tables that tables.sql returns; otherwise the relationship
-- would point at a column that is not in the metadata. That also drops FKs to or
-- from system schemas, extension tables and partition children. In particular
-- PostgreSQL clones a partitioned table's FKs onto every partition, and clones
-- FKs that reference a partitioned table onto each of its partitions; both
-- kinds of clone touch a partition, so the relispartition tests remove them
-- and only the one real constraint per FK is returned.

SELECT
    con.conname       AS constraint_name,
    fn.nspname        AS from_schema,
    fc.relname        AS from_table,
    fa.attname        AS from_column,
    tn.nspname        AS to_schema,
    tc.relname        AS to_table,
    ta.attname        AS to_column,
    k.ord::int        AS key_position
FROM pg_constraint con
JOIN pg_class fc     ON fc.oid = con.conrelid
JOIN pg_namespace fn ON fn.oid = fc.relnamespace
JOIN pg_class tc     ON tc.oid = con.confrelid
JOIN pg_namespace tn ON tn.oid = tc.relnamespace
CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(from_attnum, to_attnum, ord)
JOIN pg_attribute fa ON fa.attrelid = fc.oid AND fa.attnum = k.from_attnum
JOIN pg_attribute ta ON ta.attrelid = tc.oid AND ta.attnum = k.to_attnum
WHERE con.contype = 'f'
  AND fc.relkind IN ('r', 'p') AND NOT fc.relispartition
  AND tc.relkind IN ('r', 'p') AND NOT tc.relispartition
  AND fn.nspname !~ '^pg_' AND fn.nspname <> 'information_schema'
  AND tn.nspname !~ '^pg_' AND tn.nspname <> 'information_schema'
  AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_class'::regclass AND d.objid IN (fc.oid, tc.oid) AND d.deptype = 'e')
ORDER BY fn.nspname, fc.relname, con.conname, k.ord;
