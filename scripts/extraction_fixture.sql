-- Fixture schema for tests/test_extraction_sql.py.
-- Each object exists to exercise one edge case in the extraction queries.

CREATE SCHEMA person;
CREATE SCHEMA sales;
CREATE SCHEMA "Mixed";                      -- exact-case names must survive

-- Plain parent table, with comments --------------------------------------
CREATE TABLE person.person (
    businessentityid int PRIMARY KEY,
    firstname        varchar(50) NOT NULL,
    lastname         varchar(50) NOT NULL,
    email            text
);
COMMENT ON TABLE  person.person           IS 'Human beings.';
COMMENT ON COLUMN person.person.firstname IS 'First name.';
-- expression unique index: must NOT make email unique
CREATE UNIQUE INDEX ux_person_email_lower ON person.person (lower(email));

-- Ordinary N:1 chain: order -> customer -> person ------------------------
CREATE TABLE sales.customer (
    customerid int PRIMARY KEY,
    personid   int REFERENCES person.person (businessentityid),
    territory  int
);
CREATE TABLE sales.salesorderheader (
    salesorderid int PRIMARY KEY,
    customerid   int NOT NULL,
    totaldue     numeric(10, 2),
    CONSTRAINT fk_soh_customer FOREIGN KEY (customerid) REFERENCES sales.customer (customerid)
);

-- Composite PK, composite FK ---------------------------------------------
CREATE TABLE sales.region (
    countryid int,
    regionid  int,
    name      text,
    PRIMARY KEY (countryid, regionid)
);
CREATE TABLE sales.store (
    storeid   int PRIMARY KEY,
    countryid int,
    regionid  int,
    CONSTRAINT fk_store_region FOREIGN KEY (countryid, regionid)
        REFERENCES sales.region (countryid, regionid)
);
-- composite FK that is itself the PK -> 1:1 only as a column SET
CREATE TABLE sales.region_detail (
    countryid int,
    regionid  int,
    PRIMARY KEY (countryid, regionid),
    CONSTRAINT fk_detail_region FOREIGN KEY (countryid, regionid)
        REFERENCES sales.region (countryid, regionid)
);

-- Single-column 1:1 (FK column has a UNIQUE constraint) -------------------
CREATE TABLE person.passport (
    passportid int PRIMARY KEY,
    personid   int UNIQUE,
    CONSTRAINT fk_passport_person FOREIGN KEY (personid) REFERENCES person.person (businessentityid)
);

-- Unique-index edge cases --------------------------------------------------
CREATE TABLE sales.promo (promoid int PRIMARY KEY, code text, active boolean);
CREATE UNIQUE INDEX ux_promo_active_code ON sales.promo (code) WHERE active;   -- partial: ignored

CREATE TABLE sales.sku (skuid int PRIMARY KEY, code text, note text);
CREATE UNIQUE INDEX ux_sku_code ON sales.sku (code) INCLUDE (note);            -- INCLUDE: ignored

-- Dropped column: ordinal_position must stay dense ------------------------
CREATE TABLE sales.dropped (a int PRIMARY KEY, b int, c int);
ALTER TABLE sales.dropped DROP COLUMN b;

-- Partitioned parent with a partition and an FK ----------------------------
CREATE TABLE sales.events (
    eventid    int,
    ts         date,
    customerid int REFERENCES sales.customer (customerid),
    PRIMARY KEY (eventid, ts)
) PARTITION BY RANGE (ts);
CREATE TABLE sales.events_2024 PARTITION OF sales.events
    FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');

-- A table that references the PARTITIONED parent: PostgreSQL clones this FK
-- onto each partition, but only one relationship should come out.
CREATE TABLE sales.event_note (
    noteid  int PRIMARY KEY,
    eventid int,
    ts      date,
    CONSTRAINT fk_note_event FOREIGN KEY (eventid, ts) REFERENCES sales.events (eventid, ts)
);

-- Mixed plain-column + expression unique index: must NOT make c look unique.
CREATE UNIQUE INDEX ux_dropped_mixed ON sales.dropped (c, (a + 1));

-- A view: must not appear as a table ---------------------------------------
CREATE VIEW sales.v_customer AS SELECT * FROM sales.customer;

-- Mixed-case, space-containing names ---------------------------------------
CREATE TABLE "Mixed"."Order Line" ("LineId" int PRIMARY KEY, "Qty" int);

-- Data for the row-estimate test (only this table is analyzed) --------------
CREATE TABLE sales.analyzed (id int PRIMARY KEY);
INSERT INTO sales.analyzed SELECT generate_series(1, 123);
ANALYZE sales.analyzed;
