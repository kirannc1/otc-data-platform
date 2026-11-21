"""Create and submit the parent fixture using the Frappe document API."""
import json
import os
from urllib.parse import quote

import httpx

from fixtures import CUSTOMERS, PRODUCTS

REQUIRED = (
    "ERP_URL", "ERP_KEY", "ERP_SECRET", "ERP_COMPANY", "ERP_CUSTOMER_GROUP",
    "ERP_TERRITORY", "ERP_ITEM_GROUP", "ERP_UOM", "ERP_WAREHOUSE_GROUP",
    "ERP_WAREHOUSE_1", "ERP_WAREHOUSE_2", "ERP_BANK", "ERP_AR",
    "ERP_INCOME", "ERP_EXPENSE", "ERP_INVENTORY", "ERP_COST_CENTER", "ERP_ORDER_REF_FIELD",
)


class ERP:
    def __init__(self):
        missing = [name for name in REQUIRED if not os.environ.get(name, "").strip()]
        if missing:
            raise RuntimeError("Missing ERP configuration: " + ", ".join(missing))
        self.cfg = {name: os.environ[name] for name in REQUIRED}
        self.http = httpx.Client(
            base_url=self.cfg["ERP_URL"].rstrip("/"), timeout=30,
            headers={"Authorization": "token " + self.cfg["ERP_KEY"] + ":" + self.cfg["ERP_SECRET"]},
        )

    def request(self, method, path, **kwargs):
        result = self.http.request(method, path, **kwargs)
        if result.is_error:
            detail = result.json() if result.headers.get("content-type", "").startswith("application/json") else {}
            raise RuntimeError(f"ERP {method} {path}: HTTP {result.status_code} {detail.get('exception', '')}")
        return result.json()

    def find(self, doc_type, filters, fields=("name",)):
        return self.request(
            "GET", "/api/resource/" + quote(doc_type),
            params={"filters": json.dumps(filters), "fields": json.dumps(fields), "limit_page_length": 100},
        )["data"]

    def get(self, doc_type, name):
        return self.request("GET", "/api/resource/" + quote(doc_type) + "/" + quote(name, safe=""))["data"]

    def ensure(self, doc_type, filters, fields, submit=False):
        rows = self.find(doc_type, filters)
        if len(rows) > 1:
            raise RuntimeError(f"Ambiguous fixture identity for {doc_type}: {filters}")
        document = (
            self.get(doc_type, rows[0]["name"]) if rows else
            self.request("POST", "/api/resource/" + quote(doc_type), json=fields)["data"]
        )
        for key, value in filters.items():
            if str(document.get(key)) != str(value):
                raise RuntimeError(f"Fixture identity mismatch in {doc_type}: {key}")
        for key in ("company", "customer", "party", "paid_from", "paid_to", "return_against"):
            if key in fields and document.get(key) != fields[key]:
                raise RuntimeError(f"Existing {doc_type} conflicts with fixture field {key}")
        for child_table, keys in (
            ("items", ("item_code", "qty", "rate", "warehouse", "t_warehouse")),
            ("references", ("reference_doctype", "reference_name", "allocated_amount")),
        ):
            if child_table in fields:
                expected = fields[child_table]
                actual = document.get(child_table, [])
                if len(expected) != len(actual):
                    raise RuntimeError(f"Existing {doc_type} conflicts with fixture {child_table}")
                for wanted, found in zip(expected, actual):
                    if any(found.get(key) != wanted[key] for key in keys if key in wanted):
                        raise RuntimeError(f"Existing {doc_type} conflicts with fixture {child_table}")
        if document.get("docstatus") == 2:
            raise RuntimeError(f"Cancelled {doc_type} fixture {document['name']}")
        if submit and document.get("docstatus") == 0:
            document = self.request(
                "POST", "/api/method/frappe.client.submit", json={"doc": document}
            )["message"]
        return document

    def preflight(self):
        c = self.cfg
        if os.environ.get("ERP_ALLOW_DEMO_WRITES") != "yes":
            raise RuntimeError("Set ERP_ALLOW_DEMO_WRITES=yes for the dedicated demo company")
        company = self.get("Company", c["ERP_COMPANY"])
        if company["default_currency"] != "INR":
            raise RuntimeError("Demo company must use INR")
        for doc_type, key in (
            ("Customer Group", "ERP_CUSTOMER_GROUP"), ("Territory", "ERP_TERRITORY"),
            ("Item Group", "ERP_ITEM_GROUP"), ("UOM", "ERP_UOM"),
            ("Warehouse", "ERP_WAREHOUSE_GROUP"), ("Cost Center", "ERP_COST_CENTER"),
            ("Account", "ERP_BANK"), ("Account", "ERP_AR"),
            ("Account", "ERP_INCOME"), ("Account", "ERP_EXPENSE"), ("Account", "ERP_INVENTORY"),
        ):
            doc = self.get(doc_type, c[key])
            if doc.get("company") and doc["company"] != c["ERP_COMPANY"]:
                raise RuntimeError(f"{key} belongs to another company")
        if c["ERP_WAREHOUSE_1"] == c["ERP_WAREHOUSE_2"]:
            raise RuntimeError("Configure two distinct parent warehouses")
        account_keys = ("ERP_BANK", "ERP_AR", "ERP_INCOME", "ERP_EXPENSE", "ERP_INVENTORY")
        if len({c[key] for key in account_keys}) != len(account_keys):
            raise RuntimeError("Configure five distinct ERP posting accounts")
        for key in account_keys:
            account = self.get("Account", c[key])
            if account.get("is_group") or account.get("disabled"):
                raise RuntimeError(f"{key} must be an active posting account")
        for doc_type in ("Sales Order", "Delivery Note", "Sales Invoice", "Stock Entry"):
            self.find(doc_type, {c["ERP_ORDER_REF_FIELD"]: "OTC-PREFLIGHT"})
        self.find("Payment Entry", {"reference_no": "OTC-PREFLIGHT"})
        self.find("GL Entry", {"voucher_no": "OTC-PREFLIGHT"})


def seed():
    api = ERP()
    try:
        return seed_documents(api)
    finally:
        api.http.close()


def seed_documents(api):
    api.preflight()
    c = api.cfg
    ref = c["ERP_ORDER_REF_FIELD"]
    common = {"company": c["ERP_COMPANY"]}
    warehouses = []
    for name in (c["ERP_WAREHOUSE_1"], c["ERP_WAREHOUSE_2"]):
        warehouses.append(api.ensure(
            "Warehouse", {**common, "warehouse_name": name},
            {**common, "warehouse_name": name, "parent_warehouse": c["ERP_WAREHOUSE_GROUP"]},
        )["name"])
    if len(set(warehouses)) != 2:
        raise RuntimeError("The two parent warehouses must be distinct")
    customers, products = [], []
    for i, (parent, *_rest) in enumerate(CUSTOMERS, 1):
        reg = CUSTOMERS[i-1][2]
        customers.append(api.ensure(
            "Customer", {"customer_name": f"OTC P-C{i} {parent}"},
            {"customer_name": f"OTC P-C{i} {parent}", "customer_type": "Company",
             "customer_group": c["ERP_CUSTOMER_GROUP"], "territory": c["ERP_TERRITORY"],
             "tax_id": reg},
        )["name"])
    for product, _, description, maker, part, *_ in PRODUCTS:
        code = "P-" + product
        products.append(api.ensure(
            "Item", {"item_code": code},
            {"item_code": code, "item_name": "OTC " + description,
             "item_group": c["ERP_ITEM_GROUP"], "stock_uom": c["ERP_UOM"],
             "is_stock_item": 1, "description": f"{maker} {part}"},
        )["name"])
    def transaction(doc_type, key, data, submit=True):
        return api.ensure(doc_type, {**common, ref: key}, {**common, ref: key, **data}, submit)

    for index, (warehouse, qty) in enumerate(zip(warehouses, (6, 4)), 1):
        transaction("Stock Entry", f"P-OPEN-{index}", {
            "stock_entry_type": "Material Receipt", "purpose": "Material Receipt",
            "posting_date": "2026-11-01", "set_posting_time": 1,
            "items": [{"item_code": products[0], "qty": qty, "t_warehouse": warehouse,
                       "basic_rate": 6000, "expense_account": c["ERP_EXPENSE"],
                       "cost_center": c["ERP_COST_CENTER"]}],
        })
    order = None
    for i, (customer, item) in enumerate(zip(customers, products), 1):
        day = "2026-11-02"
        document = transaction("Sales Order", f"P-S{i}", {
            "customer": customer, "transaction_date": day, "delivery_date": "2026-11-05",
            "currency": "INR", "items": [{"item_code": item, "qty": 10 if i == 1 else 1,
                                        "rate": 10000, "warehouse": warehouses[0],
                                        "delivery_date": "2026-11-05"}],
        }, submit=i == 1)
        if i == 1:
            order = document
    invoices = []
    for i, (warehouse, qty, day, due) in enumerate(zip(
        warehouses, (6, 4), ("2026-11-03", "2026-11-05"), ("2026-12-03", "2026-12-05")
    ), 1):
        delivery = transaction("Delivery Note", f"P-DEL{i}", {
            "customer": customers[0], "posting_date": day, "set_posting_time": 1,
            "items": [{"item_code": products[0], "qty": qty, "rate": 10000,
                       "warehouse": warehouse, "against_sales_order": order["name"],
                       "so_detail": order["items"][0]["name"]}],
        })
        invoices.append(transaction("Sales Invoice", f"P-I{i}", {
            "customer": customers[0], "currency": "INR", "debit_to": c["ERP_AR"],
            "posting_date": day, "due_date": due, "set_posting_time": 1, "update_stock": 0,
            "items": [{"item_code": products[0], "qty": qty, "rate": 10000,
                       "warehouse": warehouse, "sales_order": order["name"],
                       "so_detail": order["items"][0]["name"], "delivery_note": delivery["name"],
                       "dn_detail": delivery["items"][0]["name"], "income_account": c["ERP_INCOME"],
                       "cost_center": c["ERP_COST_CENTER"]}],
        }))
    api.ensure("Payment Entry", {**common, "reference_no": "P-P1"}, {
        **common, "reference_no": "P-P1", "reference_date": "2026-11-10",
        "posting_date": "2026-11-10", "payment_type": "Receive", "party_type": "Customer",
        "party": customers[0], "paid_from": c["ERP_AR"], "paid_to": c["ERP_BANK"],
        "paid_from_account_currency": "INR", "paid_to_account_currency": "INR",
        "paid_amount": 25000, "received_amount": 25000,
        "source_exchange_rate": 1, "target_exchange_rate": 1,
        "references": [{"reference_doctype": "Sales Invoice", "reference_name": invoices[0]["name"],
                        "total_amount": 60000, "outstanding_amount": 60000, "allocated_amount": 25000}],
    }, True)
    credit = transaction("Sales Invoice", "P-CR1", {
        "customer": customers[0], "currency": "INR", "debit_to": c["ERP_AR"],
        "posting_date": "2026-11-15", "due_date": "2026-11-15", "set_posting_time": 1,
        "is_return": 1, "return_against": invoices[1]["name"], "update_stock": 0,
        "update_outstanding_for_self": 0,
        "items": [{"item_code": products[0], "qty": -1, "rate": 5000,
                   "sales_invoice_item": invoices[1]["items"][0]["name"],
                   "income_account": c["ERP_INCOME"], "cost_center": c["ERP_COST_CENTER"]}],
    })
    for invoice in invoices:
        current = api.get("Sales Invoice", invoice["name"])
        if float(current["outstanding_amount"]) != 35000:
            raise RuntimeError(f"Expected ₹35,000 outstanding on {invoice['name']}; verify payment and credit allocation")
    revenue = 0
    for document in [*invoices, credit]:
        lines = api.find(
            "GL Entry",
            {"voucher_no": document["name"], "company": c["ERP_COMPANY"],
             "account": c["ERP_INCOME"], "is_cancelled": 0},
            ("debit", "credit"),
        )
        revenue += sum(float(row["credit"]) - float(row["debit"]) for row in lines)
    if revenue != 95000:
        raise RuntimeError(f"Expected ₹95,000 parent GL revenue; found {revenue}")
    return {"order": order["name"], "payment": "P-P1", "invoices": [i["name"] for i in invoices], "credit": credit["name"]}
