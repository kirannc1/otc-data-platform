"""Build normalized Silver rows from one set of verified OTC Bronze inputs."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import json
import re


PARENT = "parent"
DEALER = "dealer"


def key(*parts):
    """An unambiguous readable key. All native parts remain separately traceable."""
    if any(part in (None, "") for part in parts):
        raise ValueError(f"Incomplete key: {parts}")
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def optional_key(*parts):
    return key(*parts) if all(part not in (None, "") for part in parts) else None


def number(value):
    if value in (None, ""):
        return None
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"Non-finite numeric field: {value!r}")
    return result


def minor(value):
    parsed = number(value)
    return None if parsed is None else parsed / Decimal(100)


def status_code(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().casefold()).strip("_") if value else None


def fixture_alias(row):
    match = re.match(r"^OTC (P-C\d+)\b", row.get("customer_name") or "")
    return match[1] if match else None


class Model:
    def __init__(self, data, keys, settings):
        self.data = data
        self.keys = keys
        self.settings = settings
        self.tables = {}
        self.coverage = {}
        self.lineage = []
        self.exceptions = []

    def rows(self, source, name):
        return self.data[source, name]

    def emit(self, table, identity, source, collection, source_record_id, **fields):
        indexed = self.tables.setdefault(table, {})
        if identity in indexed:
            raise ValueError(f"Duplicate {table} key: {identity}")
        indexed[identity] = {"id": identity, **fields}
        self.lineage.append({"table": table, "id": identity, "source_record_id": source_record_id,
                             "bronze_key": self.keys.get((source, collection))})
        return indexed[identity]

    def flag(self, rule, table, identity, detail, severity="review"):
        self.exceptions.append({"rule": rule, "table": table, "id": identity,
                                "severity": severity, "detail": detail})

    def table(self, name):
        return self.tables.setdefault(name, {})

    def reference(self, table, candidate, holder, identity, role, required=False):
        if candidate is not None and candidate in self.table(table):
            return candidate
        if required or candidate is not None:
            self.flag("unresolved_reference", holder, identity,
                      {"role": role, "target": table, "source_key": candidate})
        return None


def normalize_orders(model):
    erp, sql = "erpnext", "dealer-sql"
    companies = model.rows(erp, "Company")
    if len(companies) != 1:
        raise ValueError("This lab expects one configured ERPNext operating company")
    company = companies[0]
    model.emit("legal_entity", PARENT, erp, "Company", company["name"],
               name=company["name"], currency=company.get("default_currency"))
    model.emit("legal_entity", DEALER, sql, "sales_orders", "dealer_legacy",
               name="Acquired dealer", currency=model.settings["dealer_currency"],
               identity_method="configured dealer database")

    masters = [
        ("source_customer", "Customer", "customers", "customer_name"),
        ("source_product", "Item", "products", "item_name"),
        ("warehouse", "Warehouse", "warehouses", "warehouse_name"),
        ("gl_account", "Account", "accounts", "account_name"),
    ]
    for table, doctype, relation, label in masters:
        for entity, source, collection in [(PARENT, erp, doctype), (DEALER, sql, relation)]:
            for raw in model.rows(source, collection):
                if entity == PARENT and raw.get("company") not in (None, "", company["name"]):
                    raise ValueError(f"Unexpected company for {doctype} {raw['name']}")
                native = raw["name"] if entity == PARENT else raw["id"]
                if entity == PARENT and table == "source_product":
                    native = raw.get("item_code") or native
                fields = {}
                if table == "source_customer":
                    fields = {"registration": raw.get("tax_id") if entity == PARENT else raw.get("registration_key"),
                              "fixture_alias": fixture_alias(raw) if entity == PARENT else native}
                elif table == "source_product":
                    fields = {"uom": raw.get("stock_uom") if entity == PARENT else raw.get("uom"),
                              "manufacturer": raw.get("manufacturer"), "part": raw.get("part") or raw.get("manufacturer_part_no")}
                else:
                    parent_field = "parent_warehouse" if table == "warehouse" else "parent_account"
                    fields = {"is_group": str(raw.get("is_group", 0)) in {"1", "True"},
                              "parent_id": optional_key(entity, raw.get(parent_field))}
                model.emit(table, key(entity, native), source, collection, native,
                           legal_entity_id=entity, source_id=native,
                           name=raw.get(label) or raw.get("name"), **fields)

    for raw in model.rows(erp, "UOM"):
        model.emit("reference_code", key("uom", raw["name"]), erp, "UOM", raw["name"],
                   domain="uom", code=raw["name"])

    for entity, source, collection in [(PARENT, erp, "Sales Order"), (DEALER, sql, "sales_orders")]:
        for raw in model.rows(source, collection):
            parent = entity == PARENT
            native = raw["name"] if parent else raw["id"]
            identity = key(entity, native)
            customer = raw.get("customer") if parent else raw.get("customer_id")
            model.emit("sales_order", identity, source, collection, native,
                       legal_entity_id=entity, source_id=native,
                       order_reference=raw.get("custom_otc_seed_id") if parent else native,
                       customer_source_id=customer,
                       source_customer_id=model.reference("source_customer", optional_key(entity, customer),
                                                          "sales_order", identity, "customer", required=True),
                       order_date=raw.get("transaction_date") if parent else raw.get("order_date"),
                       currency=raw.get("currency") if parent else model.settings["dealer_currency"],
                       source_status=raw.get("status"), status_code=status_code(raw.get("status")),
                       docstatus=raw.get("docstatus"))

    def order_line(entity, source, collection, order, row):
        parent = entity == PARENT
        native = row["name"] if parent else row["id"]
        identity = key(entity, order, native)
        product = row.get("item_code") if parent else row.get("product_id")
        product_id = model.reference("source_product", optional_key(entity, product),
                                     "sales_order_line", identity, "product", required=True)
        model.emit("sales_order_line", identity, source, collection, key(order, native),
                   legal_entity_id=entity, source_id=native,
                   sales_order_id=model.reference("sales_order", key(entity, order),
                                                  "sales_order_line", identity, "header", required=True),
                   product_source_id=product, source_product_id=product_id,
                   planned_warehouse_id=model.reference("warehouse", optional_key(entity, row.get("warehouse")),
                                                        "sales_order_line", identity, "planned_warehouse"),
                   quantity=number(row.get("qty") if parent else row.get("quantity")),
                   uom=row.get("uom") if parent else model.table("source_product").get(product_id, {}).get("uom"),
                   uom_basis="transaction" if parent else "dealer fixture uses master unit",
                   unit_price=number(row.get("rate")) if parent else None,
                   amount=number(row.get("amount")) if parent else minor(row.get("amount_minor")))

    for order in model.rows(erp, "Sales Order"):
        for line in order.get("items", []):
            order_line(PARENT, erp, "Sales Order", order["name"], line)
    for line in model.rows(sql, "order_lines"):
        order_line(DEALER, sql, "order_lines", line["order_id"], line)


def apply_mdm(model):
    effective_from = date.fromisoformat(model.settings["mdm_effective_from"]).isoformat()
    approved = model.settings.get("mdm_steward_decisions", {})
    remaining = set(approved)
    aliases = defaultdict(list)
    for domain in ("customer", "product"):
        for row in model.table(f"source_{domain}").values():
            alias = row.get("fixture_alias") if domain == "customer" else row["source_id"]
            if alias:
                aliases[domain, row["legal_entity_id"], alias].append(row["id"])

    mapped = set()
    for row in model.rows("mdm", "candidates"):
        domain = row["domain"]
        parent_alias, dealer_alias = row["parent"], row["dealer"]
        candidate = key(domain, parent_alias, dealer_alias)
        decision = approved.get(candidate)
        state = row["status"]
        if state not in {"approved", "pending", "separate"}:
            raise ValueError(f"Unknown MDM fixture decision: {state}")
        valid_from = effective_from
        if decision is not None:
            remaining.remove(candidate)
            if state != "pending" or not decision.get("reviewer") or not decision.get("evidence"):
                raise ValueError(f"Only pending candidates with reviewer and evidence can be approved: {candidate}")
            valid_from = date.fromisoformat(decision["effective_from"]).isoformat()
            state = "approved"
        model.emit("match_candidate", candidate, "mdm", "candidates", candidate,
                   domain=domain, parent_alias=parent_alias, dealer_alias=dealer_alias,
                   decision_status=state, reviewer=decision.get("reviewer") if decision else None,
                   evidence=decision.get("evidence") if decision else "seed-v1 candidate manifest")
        if state == "pending":
            continue
        sides = [(entity, alias, aliases[domain, entity, alias])
                 for entity, alias in ((PARENT, parent_alias), (DEALER, dealer_alias))]
        if any(len(matches) != 1 for _, _, matches in sides):
            model.flag("ambiguous_or_missing_master_alias", "match_candidate", candidate, sides)
            continue
        if domain == "customer" and row["status"] == "approved":
            registrations = [model.table("source_customer")[matches[0]].get("registration")
                             for _, _, matches in sides]
            if not all(registrations) or len({value.strip().upper() for value in registrations}) != 1:
                model.flag("customer_registration_mismatch", "match_candidate", candidate, registrations)
                continue
        for entity, _, matches in sides:
            local = matches[0]
            if local in mapped:
                raise ValueError(f"Overlapping fixture mappings for {local}")
            mapped.add(local)
            group = row["expected_dealer_group_id"] if entity == DEALER and state == "separate" else row["expected_group_id"]
            if group not in model.table(f"group_{domain}"):
                model.emit(f"group_{domain}", group, "mdm", "candidates", candidate)
            model.emit(f"{domain}_crosswalk", key(local, valid_from), "mdm", "candidates", candidate,
                       source_id=local, group_id=group, valid_from=valid_from, valid_to=None,
                       match_candidate_id=candidate)

    if remaining:
        raise ValueError(f"Unknown steward decision keys: {sorted(remaining)}")

    accounts = model.rows("mdm", "account-mappings")
    if len(accounts) != 1:
        raise ValueError("Expected one account mapping object")
    for entity, mapping in accounts[0].items():
        for account, category in mapping.items():
            local = key(entity, account)
            if local not in model.table("gl_account"):
                model.flag("unknown_mapped_account", "account_crosswalk", local, category)
                continue
            if category not in model.table("reporting_account_category"):
                model.emit("reporting_account_category", category, "mdm", "account-mappings", account)
            model.emit("account_crosswalk", key(local, effective_from), "mdm", "account-mappings", account,
                       source_id=local, group_id=category, valid_from=effective_from, valid_to=None)

    settings = model.settings.get("seal_conversion")
    if settings is not None:
        local = key(DEALER, "D-SEAL")
        mapping = next((row for row in model.table("product_crosswalk").values()
                        if row["source_id"] == local), None)
        valid_from = date.fromisoformat(settings["effective_from"]).isoformat()
        if (not mapping or valid_from < mapping["valid_from"]
                or not settings.get("reviewer") or not settings.get("evidence")):
            raise ValueError("Seal conversion requires an effective approved product match, reviewer and evidence")
        model.emit("uom_conversion", key(local, "pack", "each", valid_from), "mdm", "candidates", "D-SEAL",
                   source_product_id=local, group_product_id=mapping["group_id"], from_uom="pack",
                   to_uom="each", factor=Decimal(10), valid_from=valid_from, valid_to=None,
                   reviewer=settings["reviewer"], evidence=settings["evidence"])

    # Produce an inspectable derived view without overwriting transaction values.
    customer_map = {r["source_id"]: r for r in model.table("customer_crosswalk").values()}
    product_map = {r["source_id"]: r for r in model.table("product_crosswalk").values()}
    conversions = {r["source_product_id"]: r for r in model.table("uom_conversion").values()}
    for line in model.table("sales_order_line").values():
        order = model.table("sales_order").get(line["sales_order_id"], {})
        day = order.get("order_date") or ""
        customer = customer_map.get(order.get("source_customer_id"))
        product = product_map.get(line["source_product_id"])
        customer = customer if customer and day >= customer["valid_from"] else None
        product = product if product and day >= product["valid_from"] else None
        conversion = conversions.get(line["source_product_id"])
        unit = status_code(line["uom"])
        quantity = line["quantity"]
        normalized_unit = "each" if unit in {"nos", "each"} else unit
        if normalized_unit == "pack":
            if product and conversion and day >= conversion["valid_from"] and quantity is not None:
                quantity, normalized_unit = quantity * conversion["factor"], conversion["to_uom"]
            else:
                quantity, normalized_unit = None, None
        model.emit("order_line_conformed", line["id"], None, None, line["id"],
                   sales_order_id=line["sales_order_id"], legal_entity_id=line["legal_entity_id"],
                   group_customer_id=customer["group_id"] if customer else None,
                   group_product_id=product["group_id"] if product else None,
                   source_quantity=line["quantity"], source_uom=line["uom"],
                   conformed_quantity=quantity if product and normalized_unit else None,
                   conformed_uom=normalized_unit if product else None,
                   currency=order.get("currency"), amount=line["amount"])


def normalize_fulfillment(model):
    erp, sql = "erpnext", "dealer-sql"
    by_order_product = defaultdict(list)
    for line in model.table("sales_order_line").values():
        by_order_product[line["sales_order_id"], line["source_product_id"]].append(line["id"])

    def handling(entity, source, collection, raw, native, items, returning=False):
        prefix = "return" if returning else "shipment"
        table = "return_document" if returning else "shipment"
        header = key(entity, native)
        parent = entity == PARENT
        order = raw.get("order_id")
        if parent:
            orders = {line.get("against_sales_order") for line in items}
            order = next(iter(orders)) if len(orders) == 1 else None
        model.emit(table, header, source, collection, native, legal_entity_id=entity, source_id=native,
                   sales_order_id=model.reference("sales_order", optional_key(entity, order),
                                                  table, header, "order"),
                   posting_date=raw.get("posting_date"), docstatus=raw.get("docstatus"),
                   original_document_source_id=raw.get("return_against") or raw.get("shipment_id"))
        for line in items:
            line_id = line["name"] if parent else line["id"]
            identity = key(entity, native, line_id)
            child = f"{prefix}_line"
            item = line.get("item_code") if parent else line.get("product_id")
            product = model.reference("source_product", optional_key(entity, item), child, identity,
                                      "product", required=not returning)
            warehouse = model.reference("warehouse", optional_key(entity, line.get("warehouse") if parent
                                        else line.get("warehouse_id")), child, identity, "actual_warehouse")
            order_line = optional_key(entity, line.get("against_sales_order"), line.get("so_detail"))
            if not parent and not returning:
                options = by_order_product[optional_key(entity, order), product]
                if len(options) == 1:
                    order_line = options[0]
                else:
                    model.flag("dealer_shipment_order_line_unresolved", child, identity, options)
            original_line = optional_key(entity, raw.get("return_against"), line.get("dn_detail"))
            if returning and not parent:
                original_line = optional_key(entity, raw.get("shipment_id"), raw.get("shipment_id"))
                original = model.table("shipment_line").get(original_line, {})
                product = original.get("source_product_id")
            model.emit(child, identity, source, collection, key(native, line_id),
                       legal_entity_id=entity, document_id=header, source_id=line_id,
                       source_product_id=product, warehouse_id=warehouse,
                       shipment_line_id=model.reference("shipment_line", original_line, child, identity, "return_origin"),
                       sales_order_line_id=model.reference("sales_order_line", order_line, child, identity, "order_line"),
                       quantity=number(line.get("qty") if parent else line.get("quantity")),
                       uom=line.get("uom") if parent else model.table("source_product").get(product, {}).get("uom"))

    deliveries = model.rows(erp, "Delivery Note")
    for raw in sorted(deliveries, key=lambda row: int(row.get("is_return") or 0)):
        handling(PARENT, erp, "Delivery Note", raw, raw["name"], raw.get("items", []),
                 bool(int(raw.get("is_return") or 0)))
    for collection in ("shipments", "returns"):
        for raw in model.rows(sql, collection):
            handling(DEALER, sql, collection, raw, raw["id"], [raw], collection == "returns")
    for doc in model.rows(erp, "Stock Entry"):
        for line in doc.get("items", []):
            quantity = number(line.get("transfer_qty") or line.get("qty"))
            for field, direction in (("s_warehouse", -1), ("t_warehouse", 1)):
                warehouse = line.get(field)
                if not warehouse or quantity is None:
                    continue
                identity = key(PARENT, "Stock Entry", doc["name"], line["name"], field)
                model.emit("inventory_movement_line", identity, erp, "Stock Entry", key(doc["name"], line["name"]),
                           legal_entity_id=PARENT, source_document_id=doc["name"],
                           source_product_id=model.reference("source_product", optional_key(PARENT, line.get("item_code")),
                                                            "inventory_movement_line", identity, "product", required=True),
                           warehouse_id=model.reference("warehouse", key(PARENT, warehouse),
                                                        "inventory_movement_line", identity, "warehouse", required=True),
                           quantity=quantity * direction, uom=line.get("stock_uom") or line.get("uom"),
                           posting_date=doc.get("posting_date"), docstatus=doc.get("docstatus"))
    model.coverage["inventory_movement_line"] = (
        "ERPNext Stock Entry legs only. Shipment and return movements require verified stock ledger coverage."
    )


def normalize_finance(model):
    erp, export = "erpnext", "dealer_export"
    invoice_sets = [(PARENT, erp, "Sales Invoice"), (DEALER, export, "invoices")]
    for entity, source, collection in invoice_sets:
        parent = entity == PARENT
        for raw in model.rows(source, collection):
            native = raw["name"] if parent else raw["id"]
            identity = key(entity, native)
            order = raw.get("order_id")
            items = raw.get("items", [])
            if parent:
                orders = {line.get("sales_order") for line in items}
                order = next(iter(orders)) if len(orders) == 1 else None
            customer = raw.get("customer") if parent else raw.get("customer_id")
            credit = bool(int(raw.get("is_return") or 0)) if parent else raw.get("document_type") == "credit_note"
            model.emit("billing_document", identity, source, collection, native,
                       legal_entity_id=entity, source_id=native, source_reference=raw.get("custom_otc_seed_id") if parent else native,
                       document_type="credit_note" if credit else "invoice",
                       sales_order_id=model.reference("sales_order", optional_key(entity, order),
                                                      "billing_document", identity, "order"),
                       source_customer_id=model.reference("source_customer", optional_key(entity, customer),
                                                           "billing_document", identity, "customer"),
                       posting_date=raw.get("posting_date") if parent else raw.get("posted_on"),
                       due_date=raw.get("due_date") if parent else raw.get("due_on"),
                       currency=raw.get("currency") if parent else model.settings["dealer_currency"],
                       docstatus=raw.get("docstatus") if parent else 1,
                       signed_total=number(raw.get("grand_total")) if parent else minor(raw.get("amount_minor")),
                       source_current_outstanding=number(raw.get("outstanding_amount")) if parent else None,
                       original_invoice_source_id=raw.get("return_against") if credit else None,
                       original_invoice_id=None, line_coverage="source lines" if items else "document only")

    for row in model.table("billing_document").values():
        original = optional_key(row["legal_entity_id"], row["original_invoice_source_id"])
        row["original_invoice_id"] = model.reference("billing_document", original,
                                                     "billing_document", row["id"], "original_invoice",
                                                     required=row["document_type"] == "credit_note")

    erp_billing = model.rows(erp, "Sales Invoice")
    billing_lines = defaultdict(list)
    for doc in erp_billing:
        for row in doc.get("items", []):
            billing_lines[row["name"]].append(key(PARENT, doc["name"], row["name"]))
    for doc in erp_billing:
        for raw in doc.get("items", []):
            identity = key(PARENT, doc["name"], raw["name"])
            candidates = billing_lines.get(raw.get("sales_invoice_item"), [])
            if raw.get("sales_invoice_item") and len(candidates) != 1:
                model.flag("billing_credit_line_unresolved", "billing_document_line", identity, candidates)
            model.emit("billing_document_line", identity, erp, "Sales Invoice", key(doc["name"], raw["name"]),
                       billing_document_id=key(PARENT, doc["name"]),
                       sales_order_line_id=model.reference("sales_order_line", optional_key(PARENT, raw.get("sales_order"),
                                                            raw.get("so_detail")), "billing_document_line", identity, "order_line"),
                       shipment_line_id=model.reference("shipment_line", optional_key(PARENT, raw.get("delivery_note"),
                                                        raw.get("dn_detail")), "billing_document_line", identity, "shipment_line"),
                       source_product_id=model.reference("source_product", optional_key(PARENT, raw.get("item_code")),
                                                         "billing_document_line", identity, "product"),
                       original_billing_line_id=candidates[0] if len(candidates) == 1 else None,
                       quantity=number(raw.get("qty")), uom=raw.get("uom"), amount=number(raw.get("amount")))

    for entity, source, collection in [(PARENT, erp, "Payment Entry"), (DEALER, export, "receipts")]:
        for raw in model.rows(source, collection):
            parent = entity == PARENT
            native = raw["name"] if parent else raw["id"]
            identity = key(entity, native)
            payment_type = raw.get("payment_type") if parent else "Receive"
            model.emit("payment", identity, source, collection, native,
                       legal_entity_id=entity, source_id=native, payment_type=payment_type,
                       source_customer_id=model.reference("source_customer",
                           optional_key(entity, raw.get("party") if raw.get("party_type") == "Customer" else None),
                           "payment", identity, "customer"),
                       payment_reference=raw.get("reference_no") if parent else raw.get("payment_reference"),
                       posting_date=raw.get("posting_date") if parent else raw.get("posted_on"),
                       currency=(raw.get("paid_to_account_currency") if payment_type == "Receive"
                                 else raw.get("paid_from_account_currency")) if parent else model.settings["dealer_currency"],
                       amount=number(raw.get("received_amount") if payment_type == "Receive" else raw.get("paid_amount"))
                       if parent else minor(raw.get("amount_minor")),
                       docstatus=raw.get("docstatus") if parent else 1)
            if not parent:
                continue
            for reference in raw.get("references", []):
                if reference.get("reference_doctype") != "Sales Invoice":
                    model.flag("allocation_type_outside_billing", "payment", identity, reference.get("reference_doctype"))
                    continue
                allocation = key(PARENT, native, reference["name"])
                model.emit("payment_allocation", allocation, source, collection, key(native, reference["name"]),
                           payment_id=identity,
                           billing_document_id=model.reference("billing_document",
                               optional_key(PARENT, reference.get("reference_name")),
                               "payment_allocation", allocation, "invoice", required=True),
                           allocated_amount=number(reference.get("allocated_amount")))

    for row in model.rows(export, "allocations"):
        identity = key(DEALER, row["id"])
        model.emit("payment_allocation", identity, export, "allocations", row["id"],
                   payment_id=model.reference("payment", key(DEALER, row["receipt_id"]),
                                              "payment_allocation", identity, "payment", required=True),
                   billing_document_id=model.reference("billing_document", key(DEALER, row["invoice_id"]),
                                                       "payment_allocation", identity, "invoice", required=True),
                   allocated_amount=minor(row["amount_minor"]))

    for row in model.rows(export, "receivables"):
        identity = key(DEALER, row["invoice_id"], row["as_of"], "dealer export")
        model.emit("receivables_snapshot", identity, export, "receivables", row["id"],
                   legal_entity_id=DEALER, cutoff=row["as_of"], balance_source="dealer export",
                   billing_document_id=model.reference("billing_document", key(DEALER, row["invoice_id"]),
                                                       "receivables_snapshot", identity, "invoice", required=True),
                   open_amount=minor(row["open_minor"]), currency=model.settings["dealer_currency"])

    for entity, source, collection in [(PARENT, erp, "GL Entry"), (DEALER, export, "gl_entries")]:
        for raw in model.rows(source, collection):
            parent = entity == PARENT
            native = raw["name"] if parent else raw["id"]
            identity = key(entity, native)
            account = raw.get("account") if parent else raw.get("account_code")
            voucher = raw.get("voucher_no") if parent else raw.get("document_id")
            voucher_type = raw.get("voucher_type") if parent else None
            voucher_key = optional_key(entity, voucher)
            invoice = payment = None
            if parent and voucher_type == "Sales Invoice":
                invoice = model.reference("billing_document", voucher_key, "gl_posting_line", identity, "invoice")
            elif parent and voucher_type == "Payment Entry":
                payment = model.reference("payment", voucher_key, "gl_posting_line", identity, "payment")
            elif not parent:
                billing_match = voucher_key in model.table("billing_document")
                payment_match = voucher_key in model.table("payment")
                if billing_match != payment_match:
                    invoice = voucher_key if billing_match else None
                    payment = voucher_key if payment_match else None
                else:
                    model.flag("dealer_voucher_unresolved", "gl_posting_line", identity, voucher)
            model.emit("gl_posting_line", identity, source, collection, native,
                       legal_entity_id=entity, source_id=native, voucher_source_id=voucher, voucher_type=voucher_type,
                       posting_date=raw.get("posting_date") if parent else raw.get("posted_on"),
                       is_cancelled=bool(int(raw.get("is_cancelled") or 0)),
                       company_currency=model.table("legal_entity")[entity]["currency"],
                       gl_account_id=model.reference("gl_account", optional_key(entity, account),
                                                     "gl_posting_line", identity, "posting_account", required=True),
                       billing_document_id=invoice, payment_id=payment,
                       debit=number(raw.get("debit")) if parent else minor(raw.get("debit_minor")),
                       credit=number(raw.get("credit")) if parent else minor(raw.get("credit_minor")))

    model.coverage["receivables_snapshot"] = (
        "Dealer published balances only. Parent ERP current outstanding is retained on invoices, separate "
        "from the calculated balance at the configured cutoff."
    )
    model.coverage["billing_document_line"] = "ERPNext item detail only. Dealer CSV has document totals."


def normalize_channels(model, deliveries):
    known = defaultdict(list)
    for order in model.table("sales_order").values():
        if order["order_reference"]:
            known[order["legal_entity_id"], order["order_reference"]].append(order["id"])

    def emit_event(entity, source, collection, raw, native, delivery_key=None):
        identity = key(entity, native)
        options = known[entity, raw.get("order_id")]
        if len(options) != 1:
            model.flag("channel_order_link_unresolved", "order_event", identity, raw.get("order_id"))
        model.emit("order_event", identity, source, collection, native,
                   legal_entity_id=entity, source_id=native,
                   sales_order_id=options[0] if len(options) == 1 else None,
                   order_reference=raw.get("order_id"), status_code=status_code(raw.get("status")),
                   event_time=raw.get("event_time"))
        if delivery_key:
            model.lineage[-1]["bronze_key"] = delivery_key

    by_event = defaultdict(list)
    for delivery in deliveries:
        raw = delivery["event"]
        identity = delivery["bronze_key"]
        model.emit("event_delivery", identity, None, None, identity, **delivery)
        model.lineage[-1]["bronze_key"] = identity
        if raw.get("entity") != PARENT or not isinstance(raw.get("event_id"), str) or not raw["event_id"]:
            model.flag("invalid_kafka_event", "event_delivery", identity, raw.get("decode_error") or "Unexpected entity or event ID",
                       severity="error")
            continue
        by_event[raw["event_id"]].append(delivery)

    for native, observations in by_event.items():
        first = observations[0]
        if any(row["event"] != first["event"] for row in observations[1:]):
            model.flag("conflicting_kafka_event", "order_event", key(PARENT, native),
                       [row["bronze_key"] for row in observations], severity="error")
            continue
        emit_event(PARENT, None, None, first["event"], native, first["bronze_key"])
        for delivery in observations[1:]:
            model.lineage.append({"table": "order_event", "id": key(PARENT, native),
                                  "source_record_id": native, "bronze_key": delivery["bronze_key"]})
    for row in model.rows("dealer_export", "order_status"):
        emit_event(DEALER, "dealer_export", "order_status", row, row["id"])

    for source, entity in [("parent-pos", PARENT), ("dealer_export", DEALER)]:
        for raw in model.rows(source, "pos_receipts"):
            native = raw.get("receipt_id") if entity == PARENT else raw["id"]
            receipt = key(entity, native)
            model.emit("pos_receipt", receipt, source, "pos_receipts", native, legal_entity_id=entity,
                       source_id=native, branch=raw.get("branch"), sold_at=raw.get("sold_at"),
                       sales_order_id=None)
            if entity == PARENT:
                for index, line in enumerate(raw.get("lines", []), start=1):
                    identity = key(entity, native, index)
                    quantity, price = number(line.get("quantity")), minor(line.get("unit_price_minor"))
                    model.emit("pos_receipt_line", identity, source, "pos_receipts", key(native, index),
                               pos_receipt_id=receipt,
                               source_product_id=model.reference("source_product", optional_key(entity, line.get("sku")),
                                                                 "pos_receipt_line", identity, "product", required=True),
                               quantity=quantity, unit_price=price,
                               amount=quantity * price if quantity is not None and price is not None else None,
                               amount_basis="quantity times price", line_identity_basis="payload position")
                if raw.get("tender"):
                    model.emit("pos_tender", key(entity, native, "tender"), source, "pos_receipts", native,
                               pos_receipt_id=receipt, method=raw["tender"], amount=None)
    for row in model.rows("dealer_export", "pos_lines"):
        identity = key(DEALER, row["id"])
        model.emit("pos_receipt_line", identity, "dealer_export", "pos_lines", row["id"],
                   pos_receipt_id=model.reference("pos_receipt", key(DEALER, row["receipt_id"]),
                                                  "pos_receipt_line", identity, "receipt", required=True),
                   source_product_id=model.reference("source_product", key(DEALER, row["product_id"]),
                                                     "pos_receipt_line", identity, "product", required=True),
                   quantity=number(row["quantity"]), amount=minor(row["amount_minor"]), amount_basis="recorded")
    for row in model.rows("dealer_export", "pos_tenders"):
        identity = key(DEALER, row["id"])
        model.emit("pos_tender", identity, "dealer_export", "pos_tenders", row["id"],
                   pos_receipt_id=model.reference("pos_receipt", key(DEALER, row["receipt_id"]),
                                                  "pos_tender", identity, "receipt", required=True),
                   method=row["method"], amount=minor(row["amount_minor"]))

    model.coverage["storefront_orders"] = "Dealer export headers retained in Bronze. Dealer events link to the SQL sales order by native ID."
    model.coverage["pos_tender"] = "Parent tender method only. Dealer export captures tender amounts."


def normalize_planning_and_bank(model):
    references = defaultdict(list)
    for payment in model.table("payment").values():
        if payment["payment_reference"] and payment["docstatus"] == 1:
            references[payment["legal_entity_id"], payment["payment_reference"]].append(payment)
    configured = model.settings.get("bank_accounts", {})
    for entity in (PARENT, DEALER):
        setup = configured.get(entity)
        if setup:
            account_id = key(entity, setup["account_reference"])
            if not setup.get("evidence"):
                raise ValueError(f"Verified bank account configuration requires evidence for {entity}")
            model.emit("bank_account", account_id, None, None, setup["account_reference"],
                       legal_entity_id=entity, **setup)
        else:
            account_id = None
        source = f"{entity}_finance"
        for raw in model.rows(source, "bank"):
            parent = entity == PARENT
            native = raw["transaction_id"] if parent else raw["reference"]
            identity = key(entity, native)
            payment_reference = raw["payment_reference"] if parent else raw["receipt"]
            amount = minor(raw["amount_minor"]) if parent else number(raw["credit_inr"])
            currency = raw["currency"] if parent else model.settings["dealer_currency"]
            day = raw["date"] if parent else raw["booking_day"]
            model.emit("bank_statement_line", identity, source, "bank", native,
                       legal_entity_id=entity, bank_account_id=account_id, posting_date=day,
                       date_basis="statement date" if parent else "booking day",
                       source_id=native, payment_reference=payment_reference, amount=amount, currency=currency)
            candidates = [payment for payment in references[entity, payment_reference]
                          if payment["amount"] == amount and payment["currency"] == currency]
            model.emit("bank_match_candidate", identity, source, "bank", native,
                       bank_statement_line_id=identity,
                       payment_id=candidates[0]["id"] if len(candidates) == 1 else None,
                       status="review" if len(candidates) == 1 else "unresolved",
                       evidence="same legal entity, payment reference, currency and amount")
            if not account_id:
                model.flag("bank_account_not_in_feed", "bank_statement_line", identity, payment_reference)

        for raw in model.rows(source, "budget"):
            parent = entity == PARENT
            scope_type = "product_category" if parent else "local_account"
            scope = raw["category"] if parent else raw["local_account"]
            period = raw["period"] if parent else raw["month"]
            currency = raw["currency"] if parent else model.settings["dealer_currency"]
            version = model.keys[source, "budget"]
            identity = key(entity, scope_type, scope, period, currency, "plan", version)
            model.emit("budget_amount", identity, source, "budget", key(scope_type, scope, period),
                       legal_entity_id=entity, period=period, currency=currency, scope=scope,
                       scope_type=scope_type, scenario="plan", input_version=version,
                       amount=minor(raw["budget_minor"]) if parent else number(raw["plan_inr"]))

    model.coverage.update({
        "bank_match_allocation": "Bank matches remain candidates pending account and payment/GL verification and approval.",
        "bank_account": "Verified account records require explicit configured account evidence.",
        "price_list": "Catalog and agreements are outside the current Bronze inputs.",
        "source_user": "Source user master is outside the current Bronze inputs.",
    })
