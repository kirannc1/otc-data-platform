"""Preflight or seed the round-two OTC demo transactions from the source host."""

import argparse
from datetime import date, datetime
import json
import os
from decimal import Decimal
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent

REFERENCE = "P-BRONZE-R2"
DAY = "2026-11-15"


def money(value):
    return Decimal(str(value or 0))


def seed(api):
    config = api.cfg
    company = {"company": config["ERP_COMPANY"]}
    reference_field = config["ERP_ORDER_REF_FIELD"]

    def find_existing(doctype, identity):
        rows = api.find(doctype, {**company, **identity})
        if len(rows) != 1:
            raise RuntimeError(f"Expected one baseline {doctype}: {identity}")
        return api.get(doctype, rows[0]["name"])

    def post(doctype, suffix, **fields):
        identity = {**company, reference_field: f"{REFERENCE}-{suffix}"}
        return api.ensure(doctype, identity, {**identity, **fields}, submit=True)

    baseline = find_existing("Sales Order", {reference_field: "P-S1"})
    source_item = baseline["items"][0]
    warehouse = source_item["warehouse"]
    customer = {"customer": baseline["customer"]}
    stock_item = {"item_code": source_item["item_code"], "warehouse": warehouse}
    invoice_fields = {
        **customer, "currency": "INR", "debit_to": config["ERP_AR"],
        "update_stock": 0, "due_date": DAY,
    }
    revenue_fields = {"income_account": config["ERP_INCOME"], "cost_center": config["ERP_COST_CENTER"]}

    # The original fixture ships all of its opening stock.
    receipt = post(
        "Stock Entry", "STOCK", stock_entry_type="Material Receipt",
        purpose="Material Receipt", posting_date=DAY, posting_time="08:00:00",
        set_posting_time=1, items=[{
            "item_code": stock_item["item_code"], "qty": 2, "t_warehouse": warehouse,
            "basic_rate": 6000, "expense_account": config["ERP_EXPENSE"],
            "cost_center": config["ERP_COST_CENTER"],
        }],
    )
    line = {**stock_item, "qty": 2, "rate": 10000}
    order = post("Sales Order", "ORDER", **customer, currency="INR",
                 transaction_date=DAY, delivery_date=DAY,
                 items=[{**line, "delivery_date": DAY}])
    order_line = order["items"][0]["name"]
    shipment = post(
        "Delivery Note", "SHIP", **customer, posting_date=DAY,
        posting_time="09:00:00", set_posting_time=1,
        items=[{**line, "against_sales_order": order["name"], "so_detail": order_line}],
    )
    invoice = post(
        "Sales Invoice", "INVOICE", **invoice_fields, posting_date=DAY,
        posting_time="10:00:00", set_posting_time=1,
        items=[{**line, **revenue_fields, "sales_order": order["name"],
                "so_detail": order_line, "delivery_note": shipment["name"],
                "dn_detail": shipment["items"][0]["name"]}],
    )
    if money(invoice["grand_total"]) != 20000:
        raise RuntimeError("Expected a tax-free INR 20,000 invoice")
    credit = post(
        "Sales Invoice", "CREDIT", **invoice_fields, posting_date=DAY,
        posting_time="11:00:00", set_posting_time=1, is_return=1,
        return_against=invoice["name"], update_outstanding_for_self=0,
        items=[{"item_code": stock_item["item_code"], "qty": -1, "rate": 10000,
                "sales_invoice_item": invoice["items"][0]["name"], **revenue_fields}],
    )
    outstanding = money(api.get("Sales Invoice", invoice["name"])["outstanding_amount"])
    if money(credit["grand_total"]) != -10000 or outstanding != 10000:
        raise RuntimeError("Expected a credit of INR 10,000 and INR 10,000 outstanding")

    def verify_gl(voucher, expected):
        rows = api.find("GL Entry", {**company, "voucher_no": voucher, "is_cancelled": 0},
                        ("name", "account", "debit", "credit"))
        balances = {}
        for row in rows:
            account = row["account"]
            balances[account] = balances.get(account, Decimal(0)) + money(row["debit"]) - money(row["credit"])
        if sum(balances.values()) != 0 or any(balances.get(account) != amount for account, amount in expected.items()):
            raise RuntimeError(f"Unexpected GL postings for {voucher}")
        return [row["name"] for row in rows]

    invoice_gl = verify_gl(invoice["name"], {config["ERP_AR"]: 20000, config["ERP_INCOME"]: -20000})
    credit_gl = verify_gl(credit["name"], {config["ERP_AR"]: -10000, config["ERP_INCOME"]: 10000})
    return {
        "fixture": REFERENCE, "stock_receipt": receipt["name"], "order": order["name"],
        "shipment": shipment["name"], "invoice": invoice["name"], "credit_note": credit["name"],
        "outstanding_inr": str(outstanding), "invoice_gl": invoice_gl, "credit_gl": credit_gl,
    }


def preflight_dealer(db):
    db.execute("SELECT pg_advisory_xact_lock(921606)")
    baseline = db.execute(
        """
        SELECT o.customer_id, l.product_id, s.warehouse_id
        FROM sales_orders o
        JOIN order_lines l ON l.order_id = o.id
        JOIN shipments s ON s.order_id = o.id AND s.product_id = l.product_id
        WHERE o.id = 'D-S1' AND l.id = 'D-L1' AND s.id = 'D-DEL1'
        """
    ).fetchone()
    if baseline != ("D-C1", "D-PUMP", "D-WH1"):
        raise RuntimeError("Expected the seeded dealer order D-S1 and warehouse D-WH1")
    count = db.execute(
        "SELECT count(*) FROM accounts WHERE id IN ('110-D', '400-D')"
    ).fetchone()[0]
    if count != 2:
        raise RuntimeError("Dealer AR and sales accounts must already exist")


def seed_dealer(db, sql):
    day = date.fromisoformat(DAY)
    due = date(2026, 12, 15)
    as_of = date(2026, 12, 16)
    accepted_at = datetime.fromisoformat(f"{DAY}T09:00:00+05:30")
    order = "D-BRONZE-R2-ORDER"
    invoice = "D-BRONZE-R2-INVOICE"
    credit = "D-BRONZE-R2-CREDIT"
    gross_minor, credit_minor = 2_000_000, 1_000_000
    entries = [
        ("sales_orders", "id customer_id order_date", (order, "D-C1", day)),
        ("order_lines", "id order_id product_id quantity amount_minor",
         ("D-BRONZE-R2-LINE", order, "D-PUMP", 2, gross_minor)),
        ("shipments", "id order_id product_id quantity warehouse_id",
         ("D-BRONZE-R2-SHIP", order, "D-PUMP", 2, "D-WH1")),
        ("storefront_orders", "id customer_id accepted_at", (order, "D-C1", accepted_at)),
        ("order_status", "id order_id status event_time",
         ("D-BRONZE-R2-EVENT", order, "accepted", accepted_at)),
        ("invoices", "id order_id customer_id posted_on due_on amount_minor document_type",
         (invoice, order, "D-C1", day, due, gross_minor, "invoice")),
        ("invoices", "id order_id customer_id posted_on due_on amount_minor document_type",
         (credit, order, "D-C1", day, due, -credit_minor, "credit_note")),
        ("receivables", "id invoice_id as_of open_minor",
         ("D-BRONZE-R2-AR", invoice, as_of, gross_minor - credit_minor)),
    ]
    for index, (document, account, debit, credit_amount) in enumerate([
        (invoice, "110-D", gross_minor, 0), (invoice, "400-D", 0, gross_minor),
        (credit, "400-D", credit_minor, 0), (credit, "110-D", 0, credit_minor),
    ], 1):
        entries.append(("gl_entries", "id document_id account_code posted_on debit_minor credit_minor",
                        (f"D-BRONZE-R2-GL{index}", document, account, day, debit, credit_amount)))
    for table, columns, values in entries:
        names = sql.SQL(", ").join(map(sql.Identifier, columns.split()))
        target = sql.Identifier("public", table)
        db.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (id) DO NOTHING").format(
                target, names, sql.SQL(", ").join([sql.Placeholder()] * len(values)),
            ),
            values,
        )
        actual = db.execute(
            sql.SQL("SELECT {} FROM {} WHERE id = %s").format(names, target), (values[0],),
        ).fetchone()
        if actual != values:
            raise RuntimeError(f"Dealer fixture already exists with different values: {table}/{values[0]}")

    return {"order": order, "invoice": invoice, "credit": credit,
            "outstanding_minor": gross_minor - credit_minor, "verified_rows": len(entries)}


def seed_commerce(client):
    lines = [{"sku": "P-PUMP", "quantity": 2, "unit_price_minor": 1_000_000}]
    event = {
        "event_id": "P-BRONZE-R2-EVENT", "order_id": "P-BRONZE-R2-ORDER",
        "customer_id": "P-C1", "status": "accepted", "lines": lines,
    }
    # Independent POS sample; this is not a payment against the ERP invoice.
    receipt = {
        "receipt_id": "P-BRONZE-R2-POS", "branch": "parent-branch", "tender": "card",
        "lines": [{"sku": "P-PUMP", "quantity": 1, "unit_price_minor": 1_000_000}],
    }
    for route, payload in (("/orders", event), ("/pos/receipts", receipt)):
        response = client.post(route, json=payload)
        if response.status_code == 409:
            raise RuntimeError(f"Conflicting round-two payload at {route}. Check the existing fixture IDs.")
        response.raise_for_status()
    return {"event_id": event["event_id"], "receipt_id": receipt["receipt_id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Post the round-two demo fixture")
    args = parser.parse_args()
    env_file = SEED_DIR / "erp.env"
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
    import erp
    import httpx
    import psycopg
    from psycopg import sql

    api = erp.ERP()
    try:
        api.preflight()
        company = {"company": api.cfg["ERP_COMPANY"]}
        if len(api.find("Sales Order", {**company, api.cfg["ERP_ORDER_REF_FIELD"]: "P-S1"})) != 1:
            raise RuntimeError("Expected the parent baseline Sales Order P-S1")
        base_url = os.environ.get("COMMERCE_URL", "http://127.0.0.1:8000")
        with httpx.Client(base_url=base_url, timeout=15) as client:
            client.get("/health").raise_for_status()
            with psycopg.connect(os.environ["DEALER_SEED_DSN"], connect_timeout=10) as db:
                if db.info.dbname != "dealer_legacy":
                    raise RuntimeError("DEALER_SEED_DSN must point to dealer_legacy")
                preflight_dealer(db)
            if not args.apply:
                print("Round-two preflight passed. Use --apply to write the fixture.")
                return
            parent = seed(api)
            print(json.dumps({"erpnext": parent}), flush=True)
            with psycopg.connect(os.environ["DEALER_SEED_DSN"], connect_timeout=10) as db:
                preflight_dealer(db)
                dealer = seed_dealer(db, sql)
            print(json.dumps({"dealer": dealer}), flush=True)
            print(json.dumps({"commerce": seed_commerce(client)}), flush=True)
    finally:
        api.http.close()


if __name__ == "__main__":
    main()
