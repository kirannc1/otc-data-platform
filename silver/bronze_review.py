"""Read-only Bronze extraction and inspection for the seed-scale OTC lab."""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

DESCRIPTIONS = {
    "Company": "ERPNext legal entity and company defaults.",
    "Customer": "ERPNext customer master.",
    "Item": "ERPNext product or stock item master.",
    "UOM": "Units of measure used by item and order records.",
    "Warehouse": "ERPNext stock location master.",
    "Account": "ERPNext chart of accounts.",
    "Sales Order": "Customer sale orders and embedded order lines.",
    "Stock Entry": "Inventory movements, including seed stock receipts.",
    "Delivery Note": "ERPNext shipment documents and delivery lines.",
    "Sales Invoice": "ERPNext invoices and return invoices used as credit notes.",
    "Payment Entry": "Cash receipt or payment documents.",
    "GL Entry": "Posted general ledger lines and their source voucher references.",
    "pos_receipts": "Parent POS sales receipts.",
    "customers": "Dealer customer master.",
    "products": "Dealer product master.",
    "warehouses": "Dealer stock locations.",
    "accounts": "Dealer chart of accounts.",
    "sales_orders": "Dealer order headers.",
    "order_lines": "Dealer order detail rows.",
    "shipments": "Dealer fulfillment records.",
    "returns": "Dealer returns records.",
}

MAPPING_FIELDS = {
    "company": ("company", "legal_entity", "legal entity"),
    "customer": ("customer",),
    "product": ("product", "item", "sku"),
    "warehouse": ("warehouse",),
}


BRONZE_BUCKET = "otc-bronze"


def s3_client(endpoint, access_key, secret_key):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, connect_timeout=10, read_timeout=30),
    )


class BronzeReader:
    """List a quiet Bronze bucket and read the listed object revisions."""

    def __init__(self, client):
        self.client = client
        self.objects = {
            obj["Key"]: obj
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=BRONZE_BUCKET)
            for obj in page.get("Contents", [])
        }
        if not self.objects:
            raise ValueError(f"{BRONZE_BUCKET} contains no objects")

    def read(self, key):
        response = self.client.get_object(
            Bucket=BRONZE_BUCKET, Key=key, IfMatch=self.objects[key]["ETag"],
        )
        with response["Body"] as stream:
            return stream.read()


def open_bronze():
    from dotenv import dotenv_values

    env_file = Path(__file__).resolve().parent.parent / "ingestion-airflow" / "platform.env"
    if not env_file.is_file():
        raise FileNotFoundError(f"Missing Compose settings: {env_file}")
    settings = dotenv_values(env_file, interpolate=False, encoding="utf-8-sig")
    credentials = []
    for name in ("BRONZE_S3_ACCESS_KEY", "BRONZE_S3_SECRET_KEY"):
        value = os.environ.get(name, settings.get(name))
        if not value:
            raise ValueError(f"Set {name} in {env_file} or the notebook environment")
        credentials.append(value)
    return BronzeReader(s3_client("http://127.0.0.1:19000", *credentials))


def snapshot_bronze(output):
    bronze = BronzeReader(s3_client(
        os.environ["BRONZE_S3_ENDPOINT"],
        os.environ["BRONZE_S3_ACCESS_KEY"],
        os.environ["BRONZE_S3_SECRET_KEY"],
    ))
    output = Path(output)
    temp = output.with_name(output.name + ".part")
    try:
        with ZipFile(temp, "w", compression=ZIP_DEFLATED) as archive:
            for key in bronze.objects:
                archive.writestr(key, bronze.read(key))
        temp.replace(output)
    finally:
        temp.unlink(missing_ok=True)
    return len(bronze.objects)


def decode_file(key, data):
    suffix = PurePosixPath(key).suffix.lower()
    if suffix == ".csv":
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
        return list(reader), reader.fieldnames or []
    if suffix in {".jsonl", ".ndjson"}:
        rows = [json.loads(line) for line in data.splitlines() if line.strip()]
    elif suffix == ".json":
        value = json.loads(data)
        rows = value if isinstance(value, list) else [value]
    else:
        return None, []
    fields = sorted({field for row in rows if isinstance(row, dict) for field in row})
    return rows, fields


def inventory(bronze):
    objects = {key: obj["Size"] for key, obj in bronze.objects.items()}
    claimed = set()
    rows = []

    def entry(key, kind, source="", collection="", description="", **extra):
        claimed.add(key)
        rows.append({
            "kind": kind, "source": source, "collection": collection, "key": key,
            "bytes": objects.get(key), "description": description, **extra,
        })

    def payload(key, kind, source, collection, description, expected=None, body=None, **extra):
        if key not in objects:
            entry(key, kind, source, collection, description, status="missing", **extra)
            return
        if body is None:
            body = bronze.read(key)
        status = "ok"
        row_count = None
        fields = []
        try:
            records, fields = decode_file(key, body)
            row_count = len(records) if records is not None else None
        except (UnicodeError, ValueError, csv.Error):
            status = "decode error"
        if expected:
            if hashlib.sha256(body).hexdigest() != expected["sha256"]:
                status = "checksum mismatch"
            elif expected.get("rows") is not None and row_count != expected["rows"]:
                status = "row count mismatch"
        entry(key, kind, source, collection, description, status=status,
              rows=row_count, fields=fields, **extra)

    for key in sorted(objects):
        if not key.endswith("/manifest.json"):
            continue
        if key.startswith("batch/"):
            manifest = json.loads(bronze.read(key))
            source = manifest["source"]
            detail = {"run_id": manifest["run_id"], "published_at": manifest["published_at"]}
            entry(key, "batch manifest", source, description="Completed full snapshot.", **detail)
            for item in manifest["files"]:
                name = item["collection"]
                payload(item["key"], "snapshot", source, name, DESCRIPTIONS.get(name, ""),
                        expected=item, **detail)
        elif key.startswith("files/"):
            folder = key.rsplit("/", 1)[0]
            source = folder.rsplit("/", 1)[0].removeprefix("files/otc-inbound/")
            entry(key, "inbound manifest", source, description="Manifest for a copied inbound file set.")
            for item in json.loads(bronze.read(key))["files"]:
                original = item["key"]
                payload(f"{folder}/objects/{original}", "inbound file", source, original,
                        describe_inbound(original), expected={"sha256": item["sha256"]})
    for key in sorted(objects.keys() - claimed):
        if key.startswith("kafka/") and key.endswith(".json"):
            body = bronze.read(key)
            envelope = json.loads(body)
            detail = {field: envelope[field] for field in ("generation", "partition", "offset")}
            payload(key, "Kafka delivery", envelope["topic"], envelope["topic"],
                    "One raw Kafka delivery with Base64-encoded key and payload.", body=body, **detail)
        else:
            entry(key, "unclassified object", description="Outside the recognized committed layouts.",
                  status="unclassified")
    return rows


def describe_inbound(key):
    name = PurePosixPath(key).name.lower()
    if key.endswith("/mdm/candidates.json"):
        return "Candidate customer and product crosswalks with review status."
    for terms, description in [
        (("map", "crosswalk"), "Candidate source-to-canonical reference mapping."),
        (("policy", "rules"), "Policy or business rule reference."),
        (("journal", "ledger", "gl_entry", "gl_entries"), "Journal or ledger export."),
        (("statement", "bank"), "Bank or statement file."),
    ]:
        if any(term in name for term in terms):
            return description
    if "/finance/" in key:
        return "Inbound finance source file. Inspect its headers for business meaning."
    if "/exports/" in key:
        return "Dealer export. Inspect its headers to confirm its grain."
    return "Seed reference or inbound source file. Inspect its headers for business meaning."


def mapping_candidates(rows, domain):
    terms = MAPPING_FIELDS[domain]
    return [
        row for row in rows if row["kind"] == "inbound file"
        and (
            row["description"].startswith("Candidate")
            or any(term in PurePosixPath(row["key"]).name.lower() for term in terms)
            or any(term in field.lower() for field in row.get("fields", []) for term in terms)
        )
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(f"Extracted {snapshot_bronze(args.output)} objects from otc-bronze.")
