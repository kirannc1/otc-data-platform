"""Read-only controls for the first OTC seed. All failures exit nonzero."""
import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
from collections import Counter
from decimal import Decimal

import boto3
import psycopg
from psycopg import sql

from erp import ERP
from fixtures import CUSTOMERS, PRODUCTS

DEALER_IDS = {
    "customers": [f"D-C{i}" for i in range(1, 6)],
    "products": ["D-" + p[1] for p in PRODUCTS],
    "warehouses": ["D-WH1"],
    "accounts": ["100-D", "110-D", "120-D", "400-D", "500-D"],
    "sales_orders": [f"D-S{i}" for i in range(1, 6)],
    "order_lines": [f"D-L{i}" for i in range(1, 6)],
    "shipments": ["D-DEL1"],
    "storefront_orders": ["D-S1"],
    "order_status": ["D-E1"],
    "invoices": ["D-I1"],
    "receivables": ["D-AR1"],
    "receipts": ["D-R1"],
    "allocations": ["D-A1"],
    "gl_entries": ["D-G1", "D-G2", "D-G3", "D-G4"],
    "pos_receipts": ["D-POS1"],
    "pos_lines": ["D-PL1"],
    "pos_tenders": ["D-PT1"],
}
EXPORT_TABLES = (
    "storefront_orders", "order_lines", "order_status", "invoices", "receivables",
    "receipts", "allocations", "gl_entries", "pos_receipts", "pos_lines", "pos_tenders",
)


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def dealer(db):
    counts = {}
    for table, ids in DEALER_IDS.items():
        query = sql.SQL("SELECT id FROM {} WHERE id = ANY(%s)").format(sql.Identifier(table))
        found = {row[0] for row in db.execute(query, (ids,))}
        require(found == set(ids), f"Dealer {table}: missing {sorted(set(ids) - found)}")
        counts[table] = len(found)
    row = db.execute("""
        SELECT i.amount_minor, r.amount_minor, a.amount_minor, ar.open_minor,
               s.warehouse_id, r.payment_reference
        FROM invoices i
        JOIN allocations a ON a.invoice_id=i.id
        JOIN receipts r ON r.id=a.receipt_id
        JOIN receivables ar ON ar.invoice_id=i.id
        JOIN shipments s ON s.order_id=i.order_id
        WHERE i.id='D-I1' AND ar.as_of='2026-12-10'
    """).fetchone()
    require(row == (4000000, 1000000, 1000000, 3000000, "D-WH1", "D-P1"),
            f"Dealer invoice/receipt/AR/warehouse control failed: {row}")
    journals = db.execute("""
        SELECT document_id, sum(debit_minor), sum(credit_minor),
               sum(CASE WHEN account_code='400-D' THEN credit_minor-debit_minor ELSE 0 END)
        FROM gl_entries WHERE document_id IN ('D-I1','D-R1')
        GROUP BY document_id ORDER BY document_id
    """).fetchall()
    require(journals == [("D-I1",4000000,4000000,4000000),("D-R1",1000000,1000000,0)],
            f"Dealer GL control failed: {journals}")
    return counts


def exact_document(api, doctype, filters):
    rows = api.find(doctype, filters)
    require(len(rows) == 1, f"ERP {doctype} {filters}: expected one record, found {len(rows)}")
    return api.get(doctype, rows[0]["name"])


def parent(api):
    c = api.cfg
    company = {"company": c["ERP_COMPANY"]}
    for i, row in enumerate(CUSTOMERS, 1):
        exact_document(api, "Customer", {"customer_name": f"OTC P-C{i} {row[0]}"})
    for row in PRODUCTS:
        exact_document(api, "Item", {"item_code": "P-" + row[0]})
    warehouses = [
        exact_document(api, "Warehouse", {**company, "warehouse_name": c[key]})["name"]
        for key in ("ERP_WAREHOUSE_1", "ERP_WAREHOUSE_2")
    ]

    def fixture(doctype, key, status=1):
        doc = exact_document(api, doctype, {**company, c["ERP_ORDER_REF_FIELD"]: key})
        require(doc["docstatus"] == status, f"{key}: expected docstatus {status}")
        return doc

    orders = [fixture("Sales Order", f"P-S{i}", int(i == 1)) for i in range(1, 6)]
    invoices = []
    vouchers = []
    for i, (qty, warehouse) in enumerate(zip((6, 4), warehouses), 1):
        stock = fixture("Stock Entry", f"P-OPEN-{i}")
        delivery = fixture("Delivery Note", f"P-DEL{i}")
        invoice = fixture("Sales Invoice", f"P-I{i}")
        require(len(delivery["items"]) == len(invoice["items"]) == 1, f"P-DEL{i}: expected one line")
        line = delivery["items"][0]
        require((line["qty"],line["warehouse"],line["against_sales_order"]) ==
                (qty,warehouse,orders[0]["name"]), f"P-DEL{i}: wrong shipment")
        require(invoice["items"][0]["delivery_note"] == delivery["name"] and
                Decimal(str(invoice["grand_total"])) == qty * 10000 and
                Decimal(str(invoice["outstanding_amount"])) == 35000, f"P-I{i}: invoice/AR control failed")
        invoices.append(invoice)
        vouchers.extend([stock["name"], delivery["name"], invoice["name"]])
    credit = fixture("Sales Invoice", "P-CR1")
    require(credit["return_against"] == invoices[1]["name"] and
            Decimal(str(credit["grand_total"])) == -5000 and not credit["update_stock"],
            "Parent credit control failed")
    payment = exact_document(api, "Payment Entry", {**company, "reference_no": "P-P1"})
    require(payment["docstatus"] == 1 and len(payment["references"]) == 1 and
            payment["references"][0]["reference_name"] == invoices[0]["name"] and
            Decimal(str(payment["references"][0]["allocated_amount"])) == 25000,
            "Parent payment allocation failed")
    revenue = Decimal(0)
    gl_count = 0
    invoices_and_credit = {doc["name"] for doc in [*invoices, credit]}
    for name in [*vouchers, credit["name"], payment["name"]]:
        lines = api.find("GL Entry", {**company, "voucher_no": name, "is_cancelled": 0},
                         ("account", "debit", "credit"))
        signed = [Decimal(str(x["credit"])) - Decimal(str(x["debit"])) for x in lines]
        require(sum(signed) == 0, f"{name}: unbalanced GL")
        gl_count += len(lines)
        if name in invoices_and_credit:
            require(bool(lines), f"{name}: no GL postings")
            revenue += sum(amount for line, amount in zip(lines,signed) if line["account"] == c["ERP_INCOME"])
    require(revenue == 95000, f"Parent revenue control failed: {revenue}")
    return {"customers":5,"items":5,"warehouses":2,"orders":5,"opening_stock":2,
            "deliveries":2,"invoices":2,"credit_notes":1,"payments":1,
            "cash_allocations":1,"gl_lines_observed":gl_count,"net_revenue_inr":95000,"open_ar_inr":70000}


def kafka(context, expected_count):
    result = subprocess.run(
        ["kubectl","--context",context,"-n","otc-sources","exec","kafka-0","--",
         "/opt/kafka/bin/kafka-console-consumer.sh","--bootstrap-server","kafka:9092",
         "--topic","parent.orders.v1","--from-beginning","--timeout-ms","12000",
         "--property","print.key=true","--property","print.offset=true"],
        capture_output=True, text=True, encoding="utf-8", timeout=35,
    )
    deliveries = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        event = json.loads(fields[-1])
        if event.get("event_id") == "P-E1":
            require("P-S1" in fields[:-1], "Kafka P-E1 key is not P-S1")
            require(event["order_id"] == "P-S1" and event["customer_id"] == "P-C1"
                    and event["status"] == "accepted" and event["currency"] == "INR"
                    and event["lines"] == [{"sku":"P-PUMP","quantity":10,"unit_price_minor":1000000}],
                    "Kafka event differs from the full seed; check the earlier smoke payload")
            deliveries.append((fields[0], event))
    require(len(deliveries) == expected_count and len({x[0] for x in deliveries}) == expected_count,
            f"Kafka expected {expected_count} deliveries at distinct offsets; found {len(deliveries)}. {result.stderr[-600:]}")
    require(all(x[1] == deliveries[0][1] for x in deliveries), "Kafka replay payload differs")
    return {"distinct_events":1,"deliveries":len(deliveries)}


def storage(snapshot):
    client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], region_name="us-east-1")
    bucket = os.environ.get("INBOUND_BUCKET", "otc-inbound")

    def get(key):
        with client.get_object(Bucket=bucket, Key=key)["Body"] as data:
            return data.read()

    def manifest(key):
        objects = {}
        for entry in json.loads(get(key))["files"]:
            name = entry["key"]
            require(name not in objects, f"Duplicate manifest key: {name}")
            data = get(name)
            require(hashlib.sha256(data).hexdigest() == entry["sha256"], f"Bad checksum: {name}")
            if "rows" in entry:
                rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
                require(len(rows) == entry["rows"], f"Bad row count: {name}")
                objects[name] = rows
            else:
                objects[name] = data
        return objects

    exports = manifest(f"dealer/exports/{snapshot}/manifest.json")
    require(len(exports) == len(EXPORT_TABLES), "Expected 11 dealer CSV objects")
    for table in EXPORT_TABLES:
        rows = [rows for key, rows in exports.items() if key.rsplit("/",1)[-1].startswith(table+"-")]
        require(len(rows) == 1 and Counter(r["id"] for r in rows[0]) == Counter(DEALER_IDS[table]),
                f"Dealer export {table}: wrong fixture IDs; export after dealer seeding")
    finance = {}
    for entity in ("parent","dealer"):
        path = f"{entity}/finance/dealer-order-v1"
        files = manifest(path + "/manifest.json")
        require(set(files) == {path+"/bank.csv", path+"/budget.csv"} and
                all(len(rows)==1 for rows in files.values()), f"{entity}: expected one bank and budget row")
        finance[entity] = (files[path+"/bank.csv"][0], files[path+"/budget.csv"][0])
    pb, pu = finance["parent"]
    db, du = finance["dealer"]
    require((pb["transaction_id"],pb["payment_reference"],pb["amount_minor"],pu["budget_minor"]) ==
            ("P-B1","P-P1","2500000","12000000"), "Parent bank/budget control failed")
    require((db["reference"],db["receipt"],Decimal(db["credit_inr"]),Decimal(du["plan_inr"])) ==
            ("D-B1","D-P1",Decimal(10000),Decimal(50000)), "Dealer bank/budget control failed")
    docs = manifest("seed-v1/manifest.json")
    require(len(docs) == 6, "Expected six MDM and policy objects")
    candidates = json.loads(docs["seed-v1/mdm/candidates.json"])
    for domain in ("customer","product"):
        require(Counter(x["status"] for x in candidates if x["domain"]==domain) ==
                {"approved":2,"pending":2,"separate":1}, f"{domain}: incorrect MDM decisions")
    mappings = json.loads(docs["seed-v1/mdm/account-mappings.json"])
    require(len(mappings["parent"])==len(mappings["dealer"])==5, "Expected five account mappings per company")
    return {"dealer_csvs":11,"dealer_csv_rows":sum(map(len,exports.values())),
            "finance_csvs":4,"finance_rows":4,"mdm_policy_objects":6,"manifests":4}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, help="Dealer export Job name printed in its manifest path")
    parser.add_argument("--context", default="kind-otc")
    parser.add_argument("--deliveries", choices=(1,2), default=1, type=int)
    args = parser.parse_args()
    api = ERP()
    try:
        erp_result = parent(api)
    finally:
        api.http.close()
    with psycopg.connect(os.environ["DEALER_SEED_DSN"], connect_timeout=5) as db:
        db.execute("SET TRANSACTION READ ONLY")
        dealer_result = dealer(db)
    with psycopg.connect(os.environ["COMMERCE_VERIFY_DSN"], connect_timeout=5) as db:
        db.execute("SET TRANSACTION READ ONLY")
        order = db.execute("SELECT payload FROM orders WHERE order_id='P-S1'").fetchone()
        event = db.execute("SELECT payload,published FROM outbox WHERE event_id='P-E1'").fetchone()
        pos = db.execute("SELECT payload FROM pos_receipts WHERE receipt_id='P-POS1'").fetchone()
    require(order and event and event[1] and order[0] == event[0] and order[0]["event_id"]=="P-E1",
            "Commerce P-S1/P-E1 is missing, inconsistent, or unpublished")
    require(pos and pos[0]["tender"]=="card" and pos[0]["lines"]==
            [{"sku":"P-PUMP","quantity":1,"unit_price_minor":1000000}], "POS P-POS1 control failed")
    print(json.dumps({"status":"PASS","erp":erp_result,"dealer":dealer_result,
                      "commerce":{"orders":1,"published_outbox":1,"pos_receipts":1},
                      "kafka":kafka(args.context,args.deliveries),"minio":storage(args.snapshot),
                      "metro_controls_inr":{"net_invoiced":135000,"posted_revenue":135000,
                                            "cash_applied":35000,"open_ar":100000}},indent=2))


if __name__ == "__main__":
    main()
