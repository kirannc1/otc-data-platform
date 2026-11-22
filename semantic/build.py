"""Build a read-only serving database from one published Gold Delta release."""
import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import duckdb
from deltalake import DeltaTable

DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(DIRECTORY.parent / "gold"))
from gold_delta import table_digest
from gold_pipeline import minio_connection

TABLES = (
    "gold_sales_daily", "gold_customer_360", "gold_orders_live", "gold_ar_aging",
    "gold_collections_monthly", "gold_pnl_vs_budget", "gold_revenue_reconciliation",
    "gold_revenue_unlinked_postings", "fact_sales_order_booking", "fact_shipment_line",
    "dim_customer", "dim_product", "gold_validation",
)
RELEASE_ID = re.compile(r"[0-9a-f]{64}")


def open_release(location):
    if location.startswith("s3://"):
        uri = urlsplit(location)
        parts = uri.path.strip("/").split("/")
        if (len(parts) != 3 or parts[0] != "releases" or parts[2] != "manifest.json"
                or not RELEASE_ID.fullmatch(parts[1]) or not uri.netloc or uri.query or uri.fragment):
            raise ValueError("Use the exact Gold manifest URI printed by notebook 14")
        client, options = minio_connection("GOLD")
        with client.get_object(Bucket=uri.netloc, Key="/".join(parts))["Body"] as body:
            manifest = json.load(body)
        if manifest.get("gold_release_id") != parts[1]:
            raise ValueError("Gold release ID differs from the URI")
        return manifest, location.rsplit("/", 1)[0], options
    path = Path(location).resolve()
    if path.name != "manifest.json":
        raise ValueError("Select the Gold release manifest.json")
    with path.open(encoding="utf-8") as body:
        return json.load(body), path.parent.as_posix(), None


def build(location, database):
    manifest, root, options = open_release(location)
    if (manifest.get("format_version"), manifest.get("table_format"),
            manifest.get("table_schema_version")) != (1, "delta", 1):
        raise ValueError("Expected a Gold Delta release published by notebook 14")
    cutoff = date.fromisoformat(manifest["rules"]["cutoff"])
    missing = set(TABLES) - manifest["tables"].keys()
    # Gold can omit a dimension entirely when no facts reference it.
    if missing - {"dim_customer", "dim_product"}:
        raise ValueError(f"Gold release is missing required tables: {', '.join(sorted(missing))}")

    target = Path(database).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".semantic-", dir=target.parent) as scratch:
        staged = Path(scratch) / "semantic.duckdb"
        with duckdb.connect(str(staged)) as connection:
            connection.execute("CREATE SCHEMA gold")
            connection.execute("""
                CREATE TABLE semantic_release (gold_release_id VARCHAR, silver_run_id VARCHAR, cutoff DATE)
            """)
            connection.execute("INSERT INTO semantic_release VALUES (?, ?, ?)",
                               [manifest["gold_release_id"], manifest["silver_run_id"], cutoff])
            for name in TABLES:
                if name in missing:
                    connection.execute(f"CREATE TABLE gold.{name} (key VARCHAR, business_id VARCHAR)")
                    continue
                spec = manifest["tables"][name]
                version = spec["version"]
                if spec["path"] != f"tables/{name}" or type(version) is not int or version < 0:
                    raise ValueError(f"Invalid Gold table specification for {name}")
                table = DeltaTable(f"{root}/{spec['path']}", version=version,
                                   storage_options=options).to_pyarrow_table()
                if table.num_rows != spec["rows"] or table_digest(table.to_pylist()) != spec["rows_sha256"]:
                    raise ValueError(f"Gold table differs from the manifest: {name}")
                connection.register("pinned_delta", table)
                connection.execute(f"CREATE TABLE gold.{name} AS SELECT * FROM pinned_delta")
                connection.unregister("pinned_delta")
            controls, failed = connection.execute("""
                SELECT count(*), count(*) FILTER (WHERE passed IS DISTINCT FROM true)
                FROM gold.gold_validation
            """).fetchone()
            if not controls or failed:
                raise ValueError("A Gold release with passing controls is required")
            connection.execute((DIRECTORY / "views.sql").read_text(encoding="utf-8"))
        os.replace(staged, target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Exact Gold Delta manifest URI")
    parser.add_argument("--database", default=str(DIRECTORY / "otc.duckdb"))
    args = parser.parse_args()
    print(build(args.manifest, args.database))
