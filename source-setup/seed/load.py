"""Run from a host with port forwards to the local source namespace."""
import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

import boto3
import httpx
import psycopg

import erp
from verify import dealer as verify_dealer
from fixtures import CUSTOMERS, PRODUCTS


def dealer():
    with psycopg.connect(os.environ["DEALER_SEED_DSN"]) as db:
        db.execute("SELECT pg_advisory_xact_lock(921606)")
        db.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS registration_key text")
        for definition in ("manufacturer text", "part text", "uom text"):
            db.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS " + definition)
        db.execute("CREATE TABLE IF NOT EXISTS warehouses (id text PRIMARY KEY, name text)")
        db.execute("GRANT SELECT ON warehouses TO dealer_reader")
        db.execute("ALTER TABLE shipments ADD COLUMN IF NOT EXISTS warehouse_id text REFERENCES warehouses(id)")
        db.execute("INSERT INTO warehouses VALUES ('D-WH1','Dealer distribution') ON CONFLICT DO NOTHING")
        db.execute("UPDATE shipments SET warehouse_id='D-WH1' WHERE id='D-DEL1'")
        db.execute("INSERT INTO accounts VALUES ('120-D','Inventory'),('500-D','COGS') ON CONFLICT DO NOTHING")
        for i, (_, name, _, registration, _) in enumerate(CUSTOMERS, 1):
            db.execute(
                "INSERT INTO customers VALUES (%s,%s,%s) ON CONFLICT (id) DO UPDATE "
                "SET name=excluded.name,registration_key=excluded.registration_key",
                (f"D-C{i}", name, registration),
            )
        for i, (_, code, label, maker, part, _, unit, _) in enumerate(PRODUCTS, 1):
            item = "D-" + code
            db.execute(
                "INSERT INTO products(id,name,manufacturer,part,uom) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET name=excluded.name,manufacturer=excluded.manufacturer,"
                "part=excluded.part,uom=excluded.uom",
                (item, "Hydraulic hose" if code == "HOSE" else label,
                 None if code == "FLT" else maker, "H900" if code == "HOSE" else part, unit),
            )
            if i > 1:
                db.execute(
                    "INSERT INTO sales_orders VALUES (%s,%s,'2026-11-06') ON CONFLICT DO NOTHING",
                    (f"D-S{i}", f"D-C{i}"),
                )
                db.execute(
                    "INSERT INTO order_lines VALUES (%s,%s,%s,1,1000000) ON CONFLICT DO NOTHING",
                    (f"D-L{i}", f"D-S{i}", item),
                )
        totals = db.execute(
            "SELECT (SELECT amount_minor FROM invoices WHERE id='D-I1'),"
            "(SELECT open_minor FROM receivables WHERE id='D-AR1'),"
            "(SELECT SUM(credit_minor-debit_minor) FROM gl_entries WHERE account_code='400-D')"
        ).fetchone()
        verify_dealer(db)
        if totals != (4000000, 3000000, 4000000):
            raise RuntimeError(f"Unexpected dealer control totals: {totals}")
    print("Dealer master and order fixtures verified")


def commerce():
    with httpx.Client(base_url=os.environ.get("COMMERCE_URL", "http://127.0.0.1:8000"), timeout=15) as client:
        client.get("/health").raise_for_status()
        for route, payload in [
            ("/orders", {"event_id": "P-E1", "order_id": "P-S1", "customer_id": "P-C1",
                         "status": "accepted", "lines": [{"sku": "P-PUMP", "quantity": 10,
                                                           "unit_price_minor": 1000000}]}),
            ("/pos/receipts", {"receipt_id": "P-POS1", "branch": "parent-branch", "tender": "card",
                               "lines": [{"sku": "P-PUMP", "quantity": 1, "unit_price_minor": 1000000}]}),
        ]:
            response = client.post(route, json=payload)
            if response.status_code == 409:
                raise RuntimeError(f"{route} has a conflicting fixture. Use a fresh demo namespace/database.")
            response.raise_for_status()
    print("Storefront order/outbox and POS receipt verified")


def documents():
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"])
    bucket = os.environ.get("INBOUND_BUCKET", "otc-inbound")
    client.head_bucket(Bucket=bucket)
    candidates = []
    for domain, rows in (("customer", CUSTOMERS), ("product", PRODUCTS)):
        for index, row in enumerate(rows, 1):
            parent, child = (
                (f"P-C{index}", f"D-C{index}") if domain == "customer"
                else ("P-" + row[0], "D-" + row[1])
            )
            case = row[-1]
            candidates.append({"domain": domain, "parent": parent, "dealer": child,
                               "status": {"automatic": "approved", "review": "pending",
                                          "distinct": "separate"}[case],
                               "expected_group_id": f"G-{'CUST' if domain == 'customer' else 'PROD'}-{index:03}",
                               "expected_dealer_group_id": f"G-{'CUST' if domain == 'customer' else 'PROD'}-{6 if case == 'distinct' else index:03}"})
    files = {
        "mdm/candidates.json": json.dumps(candidates, indent=2),
        "mdm/evidence/customer-registration.txt": "Synthetic evidence: Apex DEMO-300; Summit DEMO-400, 4 Demo Park.",
        "mdm/evidence/supplier-catalog.txt": "Synthetic ACME catalog: F200 filter each; S20 seal pack contains 10 each.",
        "policy/ar-aging-v1.txt": "Synthetic policy. Effective 2026-11-01. Age open invoices by due date at the reporting cutoff.",
        "policy/reconciliation-v1.txt": "Synthetic policy. Effective 2026-11-01. Reconcile posted invoice and credit totals to GL per entity.",
        "mdm/account-mappings.json": json.dumps({
            "parent": {os.environ[key]: category for key, category in {
                "ERP_INCOME": "SALES_REVENUE", "ERP_AR": "TRADE_AR", "ERP_BANK": "CASH_BANK",
                "ERP_INVENTORY": "INVENTORY", "ERP_EXPENSE": "COGS",
            }.items()},
            "dealer": {"400-D": "SALES_REVENUE", "110-D": "TRADE_AR", "100-D": "CASH_BANK",
                       "120-D": "INVENTORY", "500-D": "COGS"},
        }, indent=2),
    }
    entries = []
    for name, content in files.items():
        key = "seed-v1/" + name
        data = content.encode()
        client.put_object(Bucket=bucket, Key=key, Body=data)
        stored = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        if stored != data:
            raise RuntimeError(f"Object verification failed: {key}")
        entries.append({"key": key, "sha256": hashlib.sha256(data).hexdigest()})
    client.put_object(Bucket=bucket, Key="seed-v1/manifest.json",
                      Body=json.dumps({"files": entries}).encode())
    print("MDM and policy objects verified")


def preflight(context):
    api = erp.ERP()
    try:
        api.preflight()
    finally:
        api.http.close()
    for name, database in (("DEALER_SEED_DSN", "dealer_legacy"),
                           ("COMMERCE_VERIFY_DSN", "commerce")):
        with psycopg.connect(os.environ[name], connect_timeout=5) as db:
            actual = db.execute("SELECT current_database()").fetchone()[0]
            if actual != database:
                raise RuntimeError(f"{name} points to {actual}; expected {database}")
            if database == "dealer_legacy":
                db.execute("SELECT id FROM customers LIMIT 1")
                db.execute("SELECT id FROM sales_orders LIMIT 1")
            else:
                existing = db.execute("SELECT payload FROM outbox WHERE event_id='P-E1'").fetchone()
                if existing and existing[0].get("lines") != [
                    {"sku":"P-PUMP","quantity":10,"unit_price_minor":1000000}
                ]:
                    raise RuntimeError("Existing P-E1 is the incompatible smoke fixture; use an isolated fixture database/topic")
    url = os.environ.get("COMMERCE_URL", "http://127.0.0.1:8000")
    httpx.get(url.rstrip("/") + "/health", timeout=10).raise_for_status()
    client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], region_name="us-east-1")
    client.head_bucket(Bucket=os.environ.get("INBOUND_BUCKET", "otc-inbound"))
    subprocess.run(
        ["kubectl","--context",context,"-n","otc-sources","exec","kafka-0","--",
         "/opt/kafka/bin/kafka-topics.sh","--bootstrap-server","kafka:9092",
         "--describe","--topic","parent.orders.v1"], check=True, timeout=30,
    )
    print("Preflight passed: ERP references, dealer and commerce databases, commerce HTTP, MinIO and Kafka")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=["preflight", "dealer", "erp", "commerce", "documents"])
    parser.add_argument("--context", default="kind-otc")
    args = parser.parse_args()
    if args.source == "preflight":
        preflight(args.context)
    elif args.source == "erp":
        result = erp.seed()
        Path("erp-seed-receipt.json").write_text(json.dumps(result, indent=2) + "\n")
        print("ERP documents submitted and AR verified")
    else:
        {"dealer": dealer, "commerce": commerce, "documents": documents}[args.source]()


if __name__ == "__main__":
    main()
