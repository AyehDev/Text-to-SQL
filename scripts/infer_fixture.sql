-- Fixture for tests/test_infer_fixture.py. Each object plants ONE case for inference.
-- Almost nothing is declared: the point is to see what inference finds.

CREATE SCHEMA shop;

-- Parents -------------------------------------------------------------------
CREATE TABLE shop.customer (customer_id int PRIMARY KEY, name text);
INSERT INTO shop.customer SELECT i, 'c' || i FROM generate_series(1, 50) i;

CREATE TABLE shop.product (productid int PRIMARY KEY, title text);
INSERT INTO shop.product SELECT i, 'p' || i FROM generate_series(1, 20) i;

-- EXACT name (customerid ~ customer_id once underscores are ignored) ---------
CREATE TABLE shop.orders (orderid int PRIMARY KEY, customerid int, productid int);
INSERT INTO shop.orders SELECT i, 1 + (i % 50), 1 + (i % 20) FROM generate_series(1, 200) i;

-- SUFFIX name: buyer_customer_id ends with customer_id ------------------------
CREATE TABLE shop.review (reviewid int PRIMARY KEY, buyer_customer_id int);
INSERT INTO shop.review SELECT i, 1 + (i % 50) FROM generate_series(1, 30) i;

-- TABLE-NAME pattern: salespersonid -> salesperson.businessentityid ------------
CREATE TABLE shop.salesperson (businessentityid int PRIMARY KEY);
INSERT INTO shop.salesperson SELECT generate_series(1, 8);
CREATE TABLE shop.store (storeid int PRIMARY KEY, salespersonid int);
INSERT INTO shop.store SELECT i, 1 + (i % 8) FROM generate_series(1, 12) i;

-- NEGATIVE: values fit inside product ids, but the name must not match --------
CREATE TABLE shop.productreview (productreviewid int PRIMARY KEY);
INSERT INTO shop.productreview SELECT generate_series(1, 8);

-- NEGATIVE: name matches, values do not (11 of 60 distinct values exist) -------
CREATE TABLE shop.lead (leadid int PRIMARY KEY, customerid int);
INSERT INTO shop.lead SELECT i, 39 + i FROM generate_series(1, 60) i;

-- NEGATIVE: name matches, but the column is empty -----------------------------
CREATE TABLE shop.draft (draftid int PRIMARY KEY, customerid int);

-- Already declared: must be left alone ----------------------------------------
CREATE TABLE shop.invoice (
    invoiceid  int PRIMARY KEY,
    customerid int REFERENCES shop.customer (customer_id)
);
INSERT INTO shop.invoice SELECT i, 1 + (i % 50) FROM generate_series(1, 40) i;

-- "id" is too generic to match by name: only widget_id -> widget.id is allowed --
CREATE TABLE shop.widget (id int PRIMARY KEY);
INSERT INTO shop.widget SELECT generate_series(1, 10);
CREATE TABLE shop.gadget (id int PRIMARY KEY);
INSERT INTO shop.gadget SELECT generate_series(1, 10);
CREATE TABLE shop.part (partid int PRIMARY KEY, widget_id int);
INSERT INTO shop.part SELECT i, 1 + (i % 10) FROM generate_series(1, 30) i;

-- Several possible parents: the tightest one must win ---------------------------
CREATE TABLE shop.employee (businessentityid int PRIMARY KEY);
INSERT INTO shop.employee SELECT generate_series(1, 10);
CREATE TABLE shop.person (businessentityid int PRIMARY KEY);
INSERT INTO shop.person SELECT generate_series(1, 100);
CREATE TABLE shop.pay (payid int PRIMARY KEY, businessentityid int);
INSERT INTO shop.pay SELECT i, 1 + (i % 10) FROM generate_series(1, 60) i;

-- Same key set as customer: only one direction, and a normal FK picks customer ---
CREATE TABLE shop.customer_extra (customerid int PRIMARY KEY);
INSERT INTO shop.customer_extra SELECT generate_series(1, 50);

-- rowguid is generic: identical values must NOT create a relationship ------------
CREATE TABLE shop.a (aid int PRIMARY KEY, rowguid uuid UNIQUE);
CREATE TABLE shop.b (bid int PRIMARY KEY, rowguid uuid);
INSERT INTO shop.a SELECT i, md5(i::text)::uuid FROM generate_series(1, 5) i;
INSERT INTO shop.b SELECT i, md5(i::text)::uuid FROM generate_series(1, 5) i;

ANALYZE;