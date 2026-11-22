"""Publish a validated Silver run as an immutable set of Delta tables."""
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
import re

# Preserve the schema when a known table has no rows or all-null columns.
COLUMNS = {
    "legal_entity": "id name currency identity_method",
    "source_customer": "id legal_entity_id source_id name registration fixture_alias",
    "source_product": "id legal_entity_id source_id name uom manufacturer part",
    "warehouse": "id legal_entity_id source_id name is_group parent_id",
    "gl_account": "id legal_entity_id source_id name is_group parent_id",
    "reference_code": "id domain code",
    "sales_order": "id legal_entity_id source_id order_reference customer_source_id source_customer_id order_date currency source_status status_code docstatus",
    "sales_order_line": "id legal_entity_id source_id sales_order_id product_source_id source_product_id planned_warehouse_id quantity uom uom_basis unit_price amount",
    "match_candidate": "id domain parent_alias dealer_alias decision_status reviewer evidence",
    "group_customer": "id",
    "group_product": "id",
    "customer_crosswalk": "id source_id group_id valid_from valid_to match_candidate_id",
    "product_crosswalk": "id source_id group_id valid_from valid_to match_candidate_id",
    "account_crosswalk": "id source_id group_id valid_from valid_to",
    "reporting_account_category": "id",
    "uom_conversion": "id source_product_id group_product_id from_uom to_uom factor valid_from valid_to reviewer evidence",
    "order_line_conformed": "id sales_order_id legal_entity_id group_customer_id group_product_id source_quantity source_uom conformed_quantity conformed_uom currency amount",
    "shipment": "id legal_entity_id source_id sales_order_id posting_date docstatus original_document_source_id",
    "return_document": "id legal_entity_id source_id sales_order_id posting_date docstatus original_document_source_id",
    "shipment_line": "id legal_entity_id document_id source_id source_product_id warehouse_id shipment_line_id sales_order_line_id quantity uom",
    "return_line": "id legal_entity_id document_id source_id source_product_id warehouse_id shipment_line_id sales_order_line_id quantity uom",
    "inventory_movement_line": "id legal_entity_id source_document_id source_product_id warehouse_id quantity uom posting_date docstatus",
    "billing_document": "id legal_entity_id source_id source_reference document_type sales_order_id source_customer_id posting_date due_date currency docstatus signed_total source_current_outstanding original_invoice_source_id original_invoice_id line_coverage",
    "billing_document_line": "id billing_document_id sales_order_line_id shipment_line_id source_product_id original_billing_line_id quantity uom amount",
    "payment": "id legal_entity_id source_id payment_type source_customer_id payment_reference posting_date currency amount docstatus",
    "payment_allocation": "id payment_id billing_document_id allocated_amount",
    "receivables_snapshot": "id legal_entity_id cutoff balance_source billing_document_id open_amount currency",
    "calculated_ar_balance": "billing_document_id legal_entity_id cutoff currency open_amount days_overdue aging_bucket coverage",
    "gl_posting_line": "id legal_entity_id source_id voucher_source_id voucher_type posting_date is_cancelled company_currency gl_account_id billing_document_id payment_id debit credit",
    "order_event": "id legal_entity_id source_id sales_order_id order_reference status_code event_time",
    "event_delivery": "id bronze_key topic generation partition offset ingested_at event",
    "pos_receipt": "id legal_entity_id source_id branch sold_at sales_order_id",
    "pos_receipt_line": "id pos_receipt_id source_product_id quantity unit_price amount amount_basis line_identity_basis",
    "pos_tender": "id pos_receipt_id method amount",
    "bank_account": "id legal_entity_id account_reference evidence",
    "bank_statement_line": "id legal_entity_id bank_account_id posting_date date_basis source_id payment_reference amount currency",
    "bank_match_candidate": "id bank_statement_line_id payment_id status evidence",
    "budget_amount": "id legal_entity_id period currency scope scope_type scenario input_version amount",
    "source_observation": "table id source_record_id bronze_key",
    "quality_exception": "rule table id severity detail",
    "reconciliation_control": "rule id expected actual status detail",
}
DECIMALS = set("quantity source_quantity conformed_quantity unit_price amount factor signed_total "
               "source_current_outstanding allocated_amount open_amount debit credit".split())
DATES = set("order_date posting_date due_date cutoff valid_from valid_to".split())
INTEGERS = {"docstatus", "days_overdue", "partition", "offset"}
BOOLEANS = {"is_cancelled", "is_group"}
JSON_FIELDS = {"detail", "expected", "actual", "event"}


def arrow_table(name, rows):
    import pyarrow as pa

    fields = set(COLUMNS.get(name, "id").split())
    fields.update(key for row in rows for key in row)
    arrays = {}
    for field in sorted(fields):
        values = [row.get(field) for row in rows]
        try:
            if field in DECIMALS:
                values = [None if x is None else Decimal(str(x)) for x in values]
                arrays[field] = pa.array(values, type=pa.decimal128(38, 12))
            elif field in DATES:
                arrays[field] = pa.array([date.fromisoformat(x) if x else None for x in values], type=pa.date32())
            elif field in INTEGERS:
                arrays[field] = pa.array([int(x) if x is not None and x != "" else None for x in values], type=pa.int64())
            elif field in BOOLEANS:
                arrays[field] = pa.array(values, type=pa.bool_())
            else:
                strings = [None if x is None else canonical_json(x).decode("utf-8").rstrip("\n")
                           if field in JSON_FIELDS or not isinstance(x, str) else x for x in values]
                arrays[field] = pa.array(strings, type=pa.string())
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError(f"Cannot encode Silver column {name}.{field}: {exc}") from exc
    return pa.table(arrays)


def canonical_json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8")


def content_hash(rows):
    encoded = sorted(canonical_json(row) for row in rows)
    checksum = sha256()
    for row in encoded:
        checksum.update(row)
    return checksum.hexdigest()


def build_delta_payload(provenance, payloads):
    tables, specs = {}, {}
    for name, payload in payloads.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(f"Invalid Silver table name: {name}")
        table = arrow_table(name, [json.loads(line) for line in payload.splitlines()])
        tables[name] = table
        specs[name] = {"path": f"tables/{name}", "version": 0, "rows": table.num_rows,
                       "rows_sha256": content_hash(table.to_pylist())}
    result = {key: value for key, value in provenance.items() if key not in {"model_run_id", "files"}}
    result.update(format_version=2, table_format="delta", table_schema_version=1, tables=specs)
    run_id = sha256(canonical_json(result)).hexdigest()
    return run_id, tables, specs, canonical_json({**result, "model_run_id": run_id})


def write_delta_tables(release_uri, tables, specs, storage_options=None):
    from deltalake import DeltaTable, write_deltalake

    for name, table in tables.items():
        spec = specs[name]
        uri = f'{release_uri}/{spec["path"]}'
        write_deltalake(uri, table, mode="ignore", storage_options=storage_options)
        reopened = DeltaTable(uri, version=spec["version"], storage_options=storage_options).to_pyarrow_table()
        if (reopened.num_rows != spec["rows"]
                or content_hash(reopened.to_pylist()) != spec["rows_sha256"]):
            raise ValueError(f"Existing Silver Delta table differs from its release: {uri}")
