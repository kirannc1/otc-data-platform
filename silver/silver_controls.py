"""Reconciliation at the declared grain and accounting cutoff for the OTC lab."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from silver_model import DEALER, PARENT, key

ZERO = Decimal(0)


def reconcile(model):
    cutoff = date.fromisoformat(model.settings["ar_cutoff"]).isoformat()
    controls = []

    def control(rule, identity, expected=None, actual=None, detail="", covered=True):
        status = "NOT_COVERED" if not covered else "PASS" if actual == expected else "FAIL"
        controls.append({"rule": rule, "id": identity, "expected": expected, "actual": actual,
                         "status": status, "detail": detail})

    def total(rows, field):
        values = [row.get(field) for row in rows]
        return sum(values, ZERO) if all(value is not None for value in values) else None

    def through(row):
        return bool(row.get("posting_date")) and row["posting_date"] <= cutoff

    erp, sql, exp = "erpnext", "dealer-sql", "dealer_export"
    for entity, source, order_set, line_set in [
        (PARENT, erp, "Sales Order", "Sales Order"), (DEALER, sql, "sales_orders", "order_lines")
    ]:
        orders = model.rows(source, order_set)
        lines = sum(len(row.get("items", [])) for row in orders) if entity == PARENT else len(model.rows(source, line_set))
        control("order_headers_to_bronze", entity, len(orders),
                sum(row["legal_entity_id"] == entity for row in model.table("sales_order").values()))
        control("order_lines_to_bronze", entity, lines,
                sum(row["legal_entity_id"] == entity for row in model.table("sales_order_line").values()))
    for table, planned in {
        "billing_document": len(model.rows(erp, "Sales Invoice")) + len(model.rows(exp, "invoices")),
        "billing_document_line": sum(len(row.get("items", [])) for row in model.rows(erp, "Sales Invoice")),
        "payment": len(model.rows(erp, "Payment Entry")) + len(model.rows(exp, "receipts")),
        "gl_posting_line": len(model.rows(erp, "GL Entry")) + len(model.rows(exp, "gl_entries")),
        "receivables_snapshot": len(model.rows(exp, "receivables")),
    }.items():
        control("records_to_bronze", table, planned, len(model.table(table)))

    vouchers = defaultdict(list)
    active_gl = [row for row in model.table("gl_posting_line").values()
                 if not row["is_cancelled"] and through(row)]
    for row in active_gl:
        voucher = row["voucher_source_id"]
        if voucher:
            vouchers[key(row["legal_entity_id"], row["voucher_type"] or "dealer voucher", voucher,
                         row["company_currency"] or "currency unknown")].append(row)
        else:
            control("voucher_balance", row["id"], detail="Voucher ID absent", covered=False)
    for voucher, rows in sorted(vouchers.items()):
        debit, credit = total(rows, "debit"), total(rows, "credit")
        control("voucher_balance", voucher, debit, credit,
                covered=debit is not None and credit is not None)

    account_mapping = {row["source_id"]: row for row in model.table("account_crosswalk").values()}
    billing = [row for row in model.table("billing_document").values() if row["docstatus"] == 1 and through(row)]
    revenue_gl = defaultdict(list)
    for row in active_gl:
        mapped = account_mapping.get(row["gl_account_id"])
        if (mapped and mapped["group_id"] == "SALES_REVENUE"
                and row["posting_date"] >= mapped["valid_from"]):
            revenue_gl[row["billing_document_id"]].append(row)
    for doc in billing:
        rows = revenue_gl[doc["id"]]
        debit, credit = total(rows, "debit"), total(rows, "credit")
        same_currency = all(row["company_currency"] == doc["currency"] for row in rows)
        control("signed_invoice_to_gl_revenue", doc["id"], doc["signed_total"],
                credit - debit if debit is not None and credit is not None else None,
                detail="Lab fixture has no tax. Compare signed document total to posted mapped sales revenue.",
                covered=bool(rows) and same_currency and doc["signed_total"] is not None
                and debit is not None and credit is not None)

    allocations = defaultdict(list)
    for row in model.table("payment_allocation").values():
        payment = model.table("payment").get(row["payment_id"], {})
        if payment.get("docstatus") == 1 and through(payment) and row["billing_document_id"]:
            allocations[row["billing_document_id"]].append(row)
    credits = defaultdict(list)
    ambiguous_orders = set()
    for doc in billing:
        if doc["document_type"] != "credit_note":
            continue
        if doc["original_invoice_id"]:
            credits[doc["original_invoice_id"]].append(doc)
        else:
            ambiguous_orders.add((doc["legal_entity_id"], doc["sales_order_id"]))
    balances = {}
    for doc in billing:
        if doc["document_type"] != "invoice":
            continue
        credit_amount = total(credits[doc["id"]], "signed_total")
        applied_amount = total(allocations[doc["id"]], "allocated_amount")
        eligible = (
            doc["signed_total"] is not None and credit_amount is not None and applied_amount is not None
            and (doc["legal_entity_id"], doc["sales_order_id"]) not in ambiguous_orders
            and (doc["legal_entity_id"], None) not in ambiguous_orders
        )
        open_amount = doc["signed_total"] + credit_amount - applied_amount if eligible else None
        due = doc["due_date"]
        days_overdue = (date.fromisoformat(cutoff) - date.fromisoformat(due)).days if due else None
        if days_overdue is None:
            age = None
        elif days_overdue <= 0:
            age = "current"
        elif days_overdue <= 30:
            age = "1-30"
        elif days_overdue <= 60:
            age = "31-60"
        elif days_overdue <= 90:
            age = "61-90"
        else:
            age = "91+"
        balances[doc["id"]] = {"billing_document_id": doc["id"], "legal_entity_id": doc["legal_entity_id"],
                               "cutoff": cutoff, "currency": doc["currency"], "open_amount": open_amount,
                               "days_overdue": days_overdue, "aging_bucket": age,
                               "coverage": "linked fixture documents" if eligible else "incomplete credit or balance evidence"}
    for row in model.table("receivables_snapshot").values():
        if row["cutoff"] != cutoff:
            continue
        calc = balances.get(row["billing_document_id"], {})
        computed = calc.get("open_amount")
        control("dealer_open_ar_to_export", row["id"], row["open_amount"], computed,
                detail="Compares the same cutoff only.", covered=computed is not None
                and row["open_amount"] is not None and calc.get("currency") == row["currency"])

    if cutoff == "2026-12-10" and model.settings.get("validate_fixture_baseline"):
        for entity, reference, expected in [(PARENT, "P-S1", (10, 100000, 95000, 70000)),
                                             (DEALER, "D-S1", (1, 40000, 40000, 30000))]:
            orders = [row for row in model.table("sales_order").values()
                      if row["legal_entity_id"] == entity and row["order_reference"] == reference]
            control("metro_fixture_order", reference, 1, len(orders))
            if len(orders) != 1:
                continue
            order = orders[0]["id"]
            lines = [row for row in model.table("sales_order_line").values() if row["sales_order_id"] == order]
            docs = [row for row in billing if row["sales_order_id"] == order]
            invoice_ids = {row["id"] for row in docs if row["document_type"] == "invoice"}
            docs += [row for row in billing if row["sales_order_id"] != order and row["original_invoice_id"] in invoice_ids]
            open_amounts = [balances[identity]["open_amount"] for identity in invoice_ids]
            for rule, want, got in zip(("metro_order_quantity", "metro_order_amount", "metro_net_billing", "metro_open_ar"),
                                       expected, (total(lines, "quantity"), total(lines, "amount"),
                                                  total(docs, "signed_total"),
                                                  sum(open_amounts, ZERO) if all(x is not None for x in open_amounts) else None)):
                control(rule, reference, want, got, covered=got is not None)

    item_rows, tender_rows = defaultdict(list), defaultdict(list)
    for row in model.table("pos_receipt_line").values():
        item_rows[row["pos_receipt_id"]].append(row)
    for row in model.table("pos_tender").values():
        tender_rows[row["pos_receipt_id"]].append(row)
    for receipt in model.table("pos_receipt"):
        items, tenders = item_rows[receipt], tender_rows[receipt]
        expected, actual = total(items, "amount"), total(tenders, "amount")
        control("pos_items_to_tenders", receipt, expected, actual,
                covered=bool(items and tenders) and expected is not None and actual is not None)

    return controls, list(balances.values())
