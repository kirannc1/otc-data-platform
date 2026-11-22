"""Publish a validated Gold release as typed Delta tables in MinIO."""
from datetime import date
from hashlib import sha256
import re

from gold_pipeline import VERSION, dec, manifest_bytes, minio_connection, publication_rows

# Include the columns for empty and all-null outputs as part of the release contract.
COLUMNS = {
    "fact_sales_order_booking": "id sales_order_id source_system order_id order_line_id order_reference legal_entity_id booking_date currency booked_amount source_qty source_uom booked_qty booked_uom line_count booking_date_key customer_key product_key legal_entity_key currency_key",
    "fact_shipment_line": "id shipment_id shipment_line_id sales_order_line_id shipped_qty shipped_uom line_count shipment_date_key legal_entity_key warehouse_key customer_key product_key",
    "fact_billing_document": "id legal_entity_id document_id document_reference original_invoice_id sales_order_id document_type posting_date due_date currency signed_billed_amount document_count document_date_key invoice_due_date_key customer_key legal_entity_key document_type_key currency_key",
    "fact_gl_posting": "id legal_entity_id posting_date currency gl_entry_id voucher_id voucher_type billing_document_id account_source_id reporting_category debit_amount credit_amount gl_line_count posting_date_key legal_entity_key gl_account_key account_category_key currency_key",
    "fact_payment_allocation": "id payment_id invoice_id allocation_id legal_entity_id payment_date application_date invoice_due_date allocated_cash_amount allocation_count currency customer_key payment_date_key application_date_key invoice_due_date_key legal_entity_key currency_key",
    "fact_ar_snapshot": "id invoice_id legal_entity_id as_of_date invoice_date due_date customer_key currency open_amount days_past_due aging_bucket balance_basis coverage_status as_of_date_key invoice_date_key invoice_due_date_key legal_entity_key currency_key aging_bucket_key",
    "fact_budget": "id legal_entity_id period fiscal_period scenario budget_version scope_type scope currency budget_amount fiscal_period_key legal_entity_key currency_key scenario_key budget_version_key planning_scope_key",
    "fact_order_event": "id legal_entity_id logical_event_id scoped_order_id event_timestamp_utc event_type channel event_count event_date_key event_time_key event_type_key channel_key legal_entity_key",
    "gold_sales_daily": "id day legal_entity_id currency booked_amount net_billed_amount distinct_orders gross_margin coverage",
    "gold_customer_360": "id customer_key currency as_of_date observed_net_billed_amount lifetime_net_billed_amount last_observed_order_date observed_distinct_orders inactive_120_days coverage",
    "gold_orders_live": "id window_start_utc channel accepted_distinct_orders cancellation_events",
    "gold_revenue_reconciliation": "id billing_document_id legal_entity_id currency as_of_date signed_billed_amount posted_revenue difference status amount_basis",
    "gold_collections_monthly": "id due_month legal_entity_id currency as_of_date invoices gross_due_amount eligible_due_amount cash_by_payment_posting_date collection_rate avg_days_to_pay coverage",
    "gold_pnl_vs_budget": "id legal_entity_id period fiscal_period scope_type scope scenario budget_version currency budget_amount actual_amount variance coverage as_of_date",
    "gold_validation": "rule expected actual passed",
    "gold_exceptions": "rule source_id",
}
COLUMNS["gold_ar_aging"] = COLUMNS["fact_ar_snapshot"]
COLUMNS["gold_revenue_unlinked_postings"] = COLUMNS["fact_gl_posting"]

DECIMALS = set("source_qty booked_qty shipped_qty gross_margin posted_revenue difference "
               "cash_by_payment_posting_date collection_rate avg_days_to_pay variance".split())
INTEGERS = set("days_past_due distinct_orders observed_distinct_orders accepted_distinct_orders "
               "cancellation_events invoices".split())
AUDIT = {"expected", "actual"}


def to_arrow(name, rows):
    import pyarrow as pa

    columns = "key business_id name" if name.startswith("dim_") else COLUMNS[name]
    fields = set(columns.split()) | {field for row in rows for field in row}
    arrays = {}
    for field in sorted(fields):
        values = [row.get(field) for row in rows]
        if field in AUDIT:
            values = [manifest_bytes(value).decode("utf-8").rstrip("\n") if value is not None
                      else None for value in values]
            kind = pa.string()
        elif field in DECIMALS or field.endswith("_amount"):
            values = [dec(value) for value in values]
            kind = pa.decimal128(38, 12)
        elif field == "day" or field.endswith("_date"):
            values = [date.fromisoformat(value) if isinstance(value, str) else value for value in values]
            kind = pa.date32()
        elif field in {"event_timestamp_utc", "window_start_utc"}:
            kind = pa.timestamp("us", tz="UTC")
        elif field in {"passed", "inactive_120_days"}:
            kind = pa.bool_()
        elif field in INTEGERS or field.endswith("_count"):
            kind = pa.int64()
        else:
            kind = pa.string()
        arrays[field] = pa.array(values, type=kind)
    return pa.table(arrays)


def table_digest(rows):
    checksum = sha256()
    for row in sorted(manifest_bytes(row) for row in rows):
        checksum.update(row)
    return checksum.hexdigest()


def delta_payload(gold):
    tables, specs = {}, {}
    for name, rows in sorted(publication_rows(gold).items()):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(f"Invalid Gold table name: {name}")
        table = to_arrow(name, rows)
        tables[name] = table
        specs[name] = {"path": f"tables/{name}", "version": 0, "rows": table.num_rows,
                       "rows_sha256": table_digest(table.to_pylist())}
    manifest = {
        "format_version": 1,
        "table_format": "delta",
        "table_schema_version": 1,
        "build_version": VERSION,
        "silver_run_id": gold.manifest["model_run_id"],
        "silver_rules": gold.manifest.get("rules", {}),
        "rules": {key: value for key, value in gold.settings.items() if key not in {"output_root", "gold_bucket"}},
        "tables": specs,
    }
    release_id = sha256(manifest_bytes(manifest)).hexdigest()
    return release_id, tables, specs, manifest_bytes({"gold_release_id": release_id, **manifest})


def write_tables(release_uri, tables, specs, storage_options=None):
    from deltalake import DeltaTable, write_deltalake

    for name, table in tables.items():
        spec = specs[name]
        uri = f'{release_uri}/{spec["path"]}'
        write_deltalake(uri, table, mode="ignore", storage_options=storage_options)
        pinned = DeltaTable(uri, version=spec["version"], storage_options=storage_options).to_pyarrow_table()
        if pinned.num_rows != spec["rows"] or table_digest(pinned.to_pylist()) != spec["rows_sha256"]:
            raise ValueError(f"Gold Delta table differs from the release manifest: {uri}")


def publish_to_minio(gold, *, client=None, storage_options=None):
    from botocore.exceptions import ClientError

    release_id, tables, specs, manifest = delta_payload(gold)
    bucket = gold.settings.get("gold_bucket", "otc-gold")
    if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("gold_bucket must be a valid bucket name")
    if client is None:
        client, configured_options = minio_connection("GOLD")
        if storage_options is None:
            storage_options = configured_options
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        try:
            client.create_bucket(Bucket=bucket)
        except ClientError as create_error:
            if create_error.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                raise

    prefix = f"releases/{release_id}"
    release_uri = f"s3://{bucket}/{prefix}"
    write_tables(release_uri, tables, specs, storage_options)
    object_key = f"{prefix}/manifest.json"
    try:
        client.put_object(Bucket=bucket, Key=object_key, Body=manifest,
                          ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] not in {409, 412}:
            raise
        with client.get_object(Bucket=bucket, Key=object_key)["Body"] as body:
            if body.read() != manifest:
                raise ValueError(f"Existing Gold manifest differs: {release_uri}/manifest.json") from exc
    return f"{release_uri}/manifest.json"
