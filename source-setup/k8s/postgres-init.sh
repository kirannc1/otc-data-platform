#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
\getenv commerce_password COMMERCE_PASSWORD
\getenv dealer_password DEALER_PASSWORD
CREATE ROLE commerce LOGIN PASSWORD :'commerce_password';
CREATE ROLE dealer_reader LOGIN PASSWORD :'dealer_password';
CREATE DATABASE commerce OWNER commerce;
CREATE DATABASE dealer_legacy;
CREATE DATABASE platform;
REVOKE CONNECT ON DATABASE commerce, dealer_legacy, platform FROM PUBLIC;
GRANT CONNECT ON DATABASE dealer_legacy TO dealer_reader;
\connect commerce
SET ROLE commerce;
CREATE TABLE orders (order_id text PRIMARY KEY, payload jsonb NOT NULL);
CREATE TABLE outbox (seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, event_id text UNIQUE NOT NULL,
                     payload jsonb NOT NULL, published boolean NOT NULL DEFAULT false);
CREATE TABLE pos_receipts (receipt_id text PRIMARY KEY, payload jsonb NOT NULL);
RESET ROLE;
\connect dealer_legacy
CREATE TABLE customers (id text PRIMARY KEY, name text);
CREATE TABLE products (id text PRIMARY KEY, name text);
CREATE TABLE accounts (id text PRIMARY KEY, name text);
CREATE TABLE sales_orders (id text PRIMARY KEY, customer_id text, order_date date);
CREATE TABLE shipments (id text PRIMARY KEY, order_id text, product_id text, quantity integer);
CREATE TABLE returns (id text PRIMARY KEY, shipment_id text, quantity integer);
CREATE TABLE storefront_orders (id text PRIMARY KEY, customer_id text, accepted_at timestamptz);
CREATE TABLE order_lines (id text PRIMARY KEY, order_id text, product_id text, quantity integer, amount_minor bigint);
CREATE TABLE order_status (id text PRIMARY KEY, order_id text, status text, event_time timestamptz);
CREATE TABLE invoices (id text PRIMARY KEY, order_id text, customer_id text, posted_on date, due_on date, amount_minor bigint, document_type text);
CREATE TABLE receivables (id text PRIMARY KEY, invoice_id text, as_of date, open_minor bigint);
CREATE TABLE receipts (id text PRIMARY KEY, payment_reference text, posted_on date, amount_minor bigint);
CREATE TABLE allocations (id text PRIMARY KEY, receipt_id text, invoice_id text, amount_minor bigint);
CREATE TABLE gl_entries (id text PRIMARY KEY, document_id text, account_code text, posted_on date, debit_minor bigint, credit_minor bigint);
CREATE TABLE pos_receipts (id text PRIMARY KEY, branch text, sold_at timestamptz);
CREATE TABLE pos_lines (id text PRIMARY KEY, receipt_id text, product_id text, quantity integer, amount_minor bigint);
CREATE TABLE pos_tenders (id text PRIMARY KEY, receipt_id text, method text, amount_minor bigint);
INSERT INTO customers VALUES ('D-C1','Metro Industrial Services');
INSERT INTO products VALUES ('D-PUMP','Replacement pump');
INSERT INTO accounts VALUES ('400-D','Sales'),('110-D','Receivables'),('100-D','Bank');
INSERT INTO sales_orders VALUES ('D-S1','D-C1','2026-11-05');
INSERT INTO shipments VALUES ('D-DEL1','D-S1','D-PUMP',1);
INSERT INTO storefront_orders VALUES ('D-S1','D-C1','2026-11-05T09:00:00+05:30');
INSERT INTO order_lines VALUES ('D-L1','D-S1','D-PUMP',1,4000000);
INSERT INTO order_status VALUES ('D-E1','D-S1','accepted','2026-11-05T09:00:00+05:30');
INSERT INTO invoices VALUES ('D-I1','D-S1','D-C1','2026-11-05','2026-12-05',4000000,'invoice');
INSERT INTO receivables VALUES ('D-AR1','D-I1','2026-12-10',3000000);
INSERT INTO receipts VALUES ('D-R1','D-P1','2026-11-12',1000000);
INSERT INTO allocations VALUES ('D-A1','D-R1','D-I1',1000000);
INSERT INTO gl_entries VALUES
 ('D-G1','D-I1','110-D','2026-11-05',4000000,0),
 ('D-G2','D-I1','400-D','2026-11-05',0,4000000),
 ('D-G3','D-R1','100-D','2026-11-12',1000000,0),
 ('D-G4','D-R1','110-D','2026-11-12',0,1000000);
INSERT INTO pos_receipts VALUES ('D-POS1','dealer-branch','2026-11-05T10:00:00+05:30');
INSERT INTO pos_lines VALUES ('D-PL1','D-POS1','D-PUMP',1,1000000);
INSERT INTO pos_tenders VALUES ('D-PT1','D-POS1','card',1000000);
GRANT USAGE ON SCHEMA public TO dealer_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dealer_reader;
SQL
