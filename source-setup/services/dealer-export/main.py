import csv
import hashlib
import io
import json
import os
from datetime import date, datetime, timezone

import boto3
import psycopg

TABLES = [
    "storefront_orders", "order_lines", "order_status", "invoices", "receivables",
    "receipts", "allocations", "gl_entries", "pos_receipts", "pos_lines", "pos_tenders",
]


def main():
    snapshot = os.environ.get("SNAPSHOT_ID") or date.today().isoformat()
    if not snapshot or not all(c.isascii() and (c.isalnum() or c in "-_") for c in snapshot):
        raise ValueError("Invalid snapshot ID")
    client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"])
    bucket = os.environ["INBOUND_BUCKET"]
    prefix = f"dealer/exports/{snapshot}"
    entries = []
    with psycopg.connect(os.environ["DEALER_DSN"]) as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cutoff = datetime.now(timezone.utc).isoformat()
        for table in TABLES:
            cursor = db.execute(f"SELECT * FROM {table} ORDER BY id")
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([column.name for column in cursor.description])
            count = 0
            for row in cursor:
                writer.writerow(row)
                count += 1
            content = output.getvalue().encode()
            checksum = hashlib.sha256(content).hexdigest()
            key = f"{prefix}/{table}-{checksum}.csv"
            client.put_object(Bucket=bucket, Key=key, Body=content)
            entries.append({"key": key, "sha256": checksum, "rows": count})
    body = json.dumps({"entity": "dealer", "cutoff": cutoff, "files": entries})
    client.put_object(Bucket=bucket, Key=f"{prefix}/manifest.json", Body=body.encode())
    print(f"Published {prefix}/manifest.json", flush=True)


if __name__ == "__main__":
    main()
