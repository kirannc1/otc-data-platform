"""Preview, preflight or apply a configurable OTC seed batch."""
import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path

from volume_plan import load_plan

ROOT = Path(__file__).resolve().parent


def doc_post(api, doctype, ref, fields):
    cfg = api.cfg
    identity = {"company": cfg["ERP_COMPANY"], cfg["ERP_ORDER_REF_FIELD"]: ref}
    doc = api.ensure(doctype, identity, {**identity, **fields}, submit=True)
    if doc["docstatus"] != 1:
        raise RuntimeError(f"Expected a submitted {doctype} for {ref}")
    for key in ("posting_date", "transaction_date", "due_date", "currency", "debit_to"):
        if key in fields and fields[key] != doc.get(key):
            raise RuntimeError(f"Unexpected ERP {key} on {ref}")
    for request, stored in zip(fields["items"], doc["items"]):
        for key in ("against_sales_order", "so_detail", "dn_detail", "sales_order", "delivery_note"):
            if key in request and request[key] != stored.get(key):
                raise RuntimeError(f"Unexpected ERP {key} on {ref}")
    return doc


def erp_masters(api, orders):
    api.preflight()
    pairs = {}
    config = api.cfg
    for order in orders:
        if order.sku in pairs:
            continue
        baseline = "P-S1" if order.sku == "P-PUMP" else "P-S2"
        rows = api.find("Sales Order", {"company": config["ERP_COMPANY"], config["ERP_ORDER_REF_FIELD"]: baseline})
        if len(rows) != 1:
            raise RuntimeError(f"Expected one baseline ERP order {baseline}")
        source = api.get("Sales Order", rows[0]["name"])
        lines = [line for line in source["items"] if line["item_code"] == order.sku and line.get("warehouse")]
        item = api.get("Item", order.sku)
        if (len(lines) != 1 or not source.get("customer") or not item.get("is_stock_item")
                or item.get("disabled") or item.get("has_serial_no") or item.get("has_batch_no")
                or item.get("stock_uom") != config["ERP_UOM"]):
            raise RuntimeError(f"Invalid demo stock item, customer or warehouse on {baseline}")
        warehouse = api.get("Warehouse", lines[0]["warehouse"])
        if warehouse["company"] != config["ERP_COMPANY"] or warehouse.get("disabled") or warehouse.get("is_group"):
            raise RuntimeError(f"Invalid posting warehouse on {baseline}")
        pairs[order.sku] = (source["customer"], warehouse["name"])
    return pairs


def check_gl(api, doc, expected):
    rows = api.find("GL Entry", {
        "company": api.cfg["ERP_COMPANY"], "voucher_no": doc["name"], "is_cancelled": 0,
    }, ("account", "debit", "credit"))
    net = Counter()
    for row in rows:
        net[row["account"]] += Decimal(str(row["debit"])) - Decimal(str(row["credit"]))
    if {account: amount for account, amount in net.items() if amount} != expected:
        raise RuntimeError(f"Unexpected GL balance for voucher {doc['name']}")


def erp_payment(api, ref, customer, invoice, amount, cash, day):
    cfg = api.cfg
    identity = {"company": cfg["ERP_COMPANY"], "reference_no": f"{ref}-PAY"}
    doc = api.ensure("Payment Entry", identity, {
        **identity, "reference_date": day, "posting_date": day, "payment_type": "Receive",
        "party_type": "Customer", "party": customer, "paid_from": cfg["ERP_AR"], "paid_to": cfg["ERP_BANK"],
        "paid_from_account_currency": "INR", "paid_to_account_currency": "INR", "paid_amount": cash,
        "received_amount": cash, "source_exchange_rate": 1, "target_exchange_rate": 1,
        "references": [{"reference_doctype": "Sales Invoice", "reference_name": invoice["name"],
                        "total_amount": amount, "outstanding_amount": amount, "allocated_amount": cash}],
    }, submit=True)
    if doc["docstatus"] != 1 or doc["posting_date"] != day:
        raise RuntimeError(f"Unexpected payment date or status on {doc['name']}")
    check_gl(api, doc, {cfg["ERP_BANK"]: cash, cfg["ERP_AR"]: -cash})


def seed_erp(api, plan, masters):
    cfg = api.cfg
    day = plan.day.isoformat()
    cost_center = cfg["ERP_COST_CENTER"]
    orders = {}
    amounts = Counter()
    for order in plan.parent:
        customer, warehouse = masters[order.sku]
        line = {"item_code": order.sku, "warehouse": warehouse, "rate": order.rate}
        doc = doc_post(api, "Sales Order", order.id, {
            "customer": customer, "currency": "INR", "transaction_date": day, "delivery_date": day,
            "items": [{**line, "qty": order.quantity, "delivery_date": day}],
        })
        if Decimal(str(doc["grand_total"])) != order.rate * order.quantity:
            raise RuntimeError(f"Expected tax-free ERP booking {order.id}")
        orders[order.id] = doc
        if order.shipments:
            doc_post(api, "Stock Entry", f"{order.id}-STOCK", {
                "stock_entry_type": "Material Receipt", "purpose": "Material Receipt",
                "posting_date": day, "posting_time": "07:00:00", "set_posting_time": 1,
                "items": [{"item_code": order.sku, "t_warehouse": warehouse, "qty": order.quantity,
                           "basic_rate": order.stock_cost, "expense_account": cfg["ERP_EXPENSE"],
                           "cost_center": cost_center}],
            })
    for order in plan.parent:
        customer, warehouse = masters[order.sku]
        booking = orders[order.id]
        for shipment in order.shipments:
            item = {"item_code": order.sku, "warehouse": warehouse, "qty": shipment.quantity, "rate": order.rate,
                    "so_detail": booking["items"][0]["name"]}
            delivery = doc_post(api, "Delivery Note", shipment.id, {
                "customer": customer, "posting_date": day, "posting_time": "09:00:00", "set_posting_time": 1,
                "items": [{**item, "against_sales_order": booking["name"]}],
            })
            if not shipment.billed:
                continue
            invoice = doc_post(api, "Sales Invoice", f"{shipment.id}-INV", {
                "customer": customer, "currency": "INR", "debit_to": cfg["ERP_AR"], "posting_date": day,
                "due_date": shipment.due.isoformat(), "posting_time": "10:00:00", "set_posting_time": 1,
                "update_stock": 0, "items": [{**item, "sales_order": booking["name"],
                    "delivery_note": delivery["name"], "dn_detail": delivery["items"][0]["name"],
                    "income_account": cfg["ERP_INCOME"], "cost_center": cost_center}],
            })
            amount = shipment.quantity * order.rate
            if Decimal(str(invoice["grand_total"])) != amount:
                raise RuntimeError(f"Expected tax-free ERP invoice {invoice['name']} to total INR {amount}")
            check_gl(api, invoice, {cfg["ERP_AR"]: amount, cfg["ERP_INCOME"]: -amount})
            if shipment.cash:
                erp_payment(api, shipment.id, customer, invoice, amount, shipment.cash, day)
            actual = Decimal(str(api.get("Sales Invoice", invoice["name"])["outstanding_amount"]))
            if actual != amount - shipment.cash:
                raise RuntimeError(f"Unexpected outstanding on {invoice['name']}: INR {actual}")
            amounts["open"] += int(actual)
    print(f"ERPNext verified {len(orders)} bookings and INR {amounts['open']} in open AR", flush=True)


def dealer_rows(plan):
    day = plan.day
    accepted = datetime.fromisoformat(f"{day.isoformat()}T09:30:00+05:30")
    for index, order in enumerate(plan.dealer):
        yield "sales_orders", "id customer_id order_date", (order.id, order.customer, day)
        yield "order_lines", "id order_id product_id quantity amount_minor", (
            f"{order.id}-L", order.id, order.sku, order.quantity, order.quantity * order.rate * 100,
        )
        if index < plan.config["storefront"]["dealer_booked_acceptances"]:
            yield "storefront_orders", "id customer_id accepted_at", (order.id, order.customer, accepted)
            yield "order_status", "id order_id status event_time", (f"{order.id}-ACCEPT", order.id, "accepted", accepted)
        for ship in order.shipments:
            yield "shipments", "id order_id product_id quantity warehouse_id", (ship.id, order.id, order.sku, ship.quantity, "D-WH1")
            if not ship.billed:
                continue
            gross, paid = ship.quantity * order.rate * 100, ship.cash * 100
            invoice = f"{ship.id}-INV"
            yield "invoices", "id order_id customer_id posted_on due_on amount_minor document_type", (
                invoice, order.id, order.customer, day, ship.due, gross, "invoice",
            )
            yield "receivables", "id invoice_id as_of open_minor", (f"{invoice}-AR", invoice, plan.cutoff, gross - paid)
            yield from dealer_gl(invoice, day, "110-D", "400-D", gross)
            if paid:
                receipt = f"{ship.id}-PAY"
                yield "receipts", "id payment_reference posted_on amount_minor", (receipt, receipt, day, paid)
                yield "allocations", "id receipt_id invoice_id amount_minor", (f"{receipt}-ALLOC", receipt, invoice, paid)
                yield from dealer_gl(receipt, day, "100-D", "110-D", paid)
    for index in range(plan.config["dealer"]["pos_receipts"]):
        ref = f"{plan.prefix('D')}-POS{index + 1:06d}"
        yield "pos_receipts", "id branch sold_at", (ref, "dealer-branch", accepted)
        yield "pos_lines", "id receipt_id product_id quantity amount_minor", (f"{ref}-L", ref, "D-PUMP", 1, 900_000)
        yield "pos_tenders", "id receipt_id method amount_minor", (f"{ref}-T", ref, "cash", 900_000)


def dealer_gl(voucher, day, debit_account, credit_account, amount):
    columns = "id document_id account_code posted_on debit_minor credit_minor"
    yield "gl_entries", columns, (f"{voucher}-GL1", voucher, debit_account, day, amount, 0)
    yield "gl_entries", columns, (f"{voucher}-GL2", voucher, credit_account, day, 0, amount)


def verify_dealer(db, plan, apply=False):
    from psycopg import sql

    if db.info.dbname != "dealer_legacy":
        raise RuntimeError("Dealer seed target must be dealer_legacy")
    db.execute("SELECT pg_advisory_xact_lock(921606)" if apply else "SET TRANSACTION READ ONLY")
    required = (("customers", "D-C1"), ("products", "D-PUMP"), ("warehouses", "D-WH1"),
                ("accounts", "110-D"), ("accounts", "400-D"), ("accounts", "100-D"))
    for table, ref in required:
        query = sql.SQL("SELECT 1 FROM {} WHERE id = %s").format(sql.Identifier("public", table))
        if db.execute(query, (ref,)).fetchone() is None:
            raise RuntimeError(f"Missing dealer master {table}/{ref}")
    total = 0
    for table, columns, values in dealer_rows(plan):
        dest = sql.Identifier("public", table)
        names = sql.SQL(", ").join(map(sql.Identifier, columns.split()))
        if apply:
            placeholders = sql.SQL(", ").join([sql.Placeholder()] * len(values))
            db.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (id) DO NOTHING").format(
                dest, names, placeholders,
            ), values)
        actual = db.execute(sql.SQL("SELECT {} FROM {} WHERE id = %s").format(names, dest), (values[0],)).fetchone()
        if actual != values and (apply or actual is not None):
            raise RuntimeError(f"Existing dealer fixture differs: {table}/{values[0]}")
        total += 1
    return total


def commerce_payloads(plan):
    def event(ref, customer, status, sku, qty, rate):
        suffix = "ACCEPT" if status == "accepted" else "CANCEL"
        return {"event_id": f"{ref}-{suffix}", "order_id": ref, "customer_id": customer, "status": status,
                "lines": [{"sku": sku, "quantity": qty, "unit_price_minor": rate * 100}]}

    settings = plan.config["storefront"]
    for order in plan.parent[:settings["parent_booked_acceptances"]]:
        yield "/orders", event(order.id, order.customer, "accepted", order.sku, order.quantity, order.rate)
    for index in range(settings["parent_cancelled_orders"]):
        ref = f"{plan.prefix('P')}-WEB{index + 1:06d}"
        for status in ("accepted", "cancelled"):
            yield "/orders", event(ref, "P-C1", status, "P-PUMP", 1, 10000)
    for index in range(plan.config["parent"]["pos_receipts"]):
        yield "/pos/receipts", {
            "receipt_id": f"{plan.prefix('P')}-POS{index + 1:06d}", "branch": "parent-branch", "tender": "card",
            "lines": [{"sku": "P-PUMP", "quantity": 1, "unit_price_minor": 1_000_000}],
        }


def seed_commerce(client, plan):
    for route, payload in commerce_payloads(plan):
        response = client.post(route, json=payload)
        identity = payload.get("event_id") or payload["receipt_id"]
        if response.status_code == 409:
            raise RuntimeError(f"Commerce fixture conflict at {route} for {identity}")
        response.raise_for_status()
        if response.status_code == 204:
            if route != "/pos/receipts":
                raise RuntimeError(f"No response payload for event {identity}")
            continue
        stored = response.json()
        if any(stored.get(key) != value for key, value in payload.items()):
            raise RuntimeError(f"Unexpected Commerce response for {identity}")


def env_from(path, allowed):
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, value = line.split("=", 1)
            if name.strip() in allowed:
                os.environ[name.strip()] = value.strip()


def reserve_batch(plan, state_dir, reserve, target):
    path = state_dir / f"{plan.config['batch_id']}.json"
    expected = {"plan_version": 1, "config": plan.config, "target": target}
    if reserve:
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as out:
                json.dump(expected, out, indent=2)
        except FileExistsError:
            pass
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != expected:
        raise ValueError(f"Batch {plan.config['batch_id']} already has a different configuration or target. Choose a new batch ID.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expect-config-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--erp-env", type=Path, default=ROOT / "erp.env")
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".volume-seed-state")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = load_plan(args.config)
    if args.expect_config_sha256 and plan.config_sha256 != args.expect_config_sha256:
        raise ValueError("Config changed after preview. Run the launcher again.")
    if args.preview:
        print(json.dumps(plan.summary(), indent=2))
        return

    import httpx
    import psycopg

    dealer_needed = bool(plan.dealer or plan.config["dealer"]["pos_receipts"])
    commerce_needed = bool(plan.summary()["parent_kafka_events"] or plan.config["parent"]["pos_receipts"])
    dsn = os.environ.get("DEALER_SEED_DSN", "").strip()
    if dealer_needed and not dsn:
        raise RuntimeError("Set DEALER_SEED_DSN or use the PowerShell launcher")
    commerce_url = os.environ.get("COMMERCE_URL", "http://127.0.0.1:8000")
    target = {"context": os.environ.get("OTC_SEED_CONTEXT", ""), "commerce_url": commerce_url if commerce_needed else None}
    with ExitStack() as stack:
        if plan.parent:
            import erp

            env_from(args.erp_env, (*erp.REQUIRED, "ERP_ALLOW_DEMO_WRITES"))
            api = erp.ERP()
            stack.callback(api.http.close)
            masters = erp_masters(api, plan.parent)
            target.update(erp_url=api.cfg["ERP_URL"], erp_company=api.cfg["ERP_COMPANY"])
        if dealer_needed:
            # Persist the identity of the target with the batch, without writing the DSN credentials.
            conn_info = psycopg.conninfo.conninfo_to_dict(dsn)
            target["dealer"] = {key: conn_info.get(key) for key in ("host", "port", "dbname")}
            with psycopg.connect(dsn, connect_timeout=10) as db:
                count = verify_dealer(db, plan)
            print(f"Dealer preflight verified the schema for {count} planned rows.", flush=True)
        if commerce_needed:
            client = stack.enter_context(httpx.Client(base_url=commerce_url, timeout=20))
            health = client.get("/health")
            health.raise_for_status()
            if health.json().get("ready") is not True:
                raise RuntimeError("Commerce did not report ready")
        reserve_batch(plan, args.state_dir, args.apply, target)
        print(f"Preflight passed for {plan.config['batch_id']}.", flush=True)
        if not args.apply:
            return
        if plan.parent:
            seed_erp(api, plan, masters)
        if dealer_needed:
            with psycopg.connect(dsn, connect_timeout=10) as db:
                verify_dealer(db, plan, apply=True)
            print(f"Dealer verified {count} rows.", flush=True)
        if commerce_needed:
            seed_commerce(client, plan)
            print("Commerce payloads accepted and verified.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
