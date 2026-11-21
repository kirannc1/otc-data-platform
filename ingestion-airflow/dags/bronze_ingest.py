"""Seed-scale source readers and raw Bronze writers."""
import base64
import hashlib
import json
import os
import re
import time
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import boto3
import psycopg
from botocore.config import Config
from botocore.exceptions import ClientError
from confluent_kafka import Consumer, KafkaError, KafkaException, TopicPartition
from psycopg import sql
from psycopg.rows import dict_row

BRONZE = "otc-bronze"
INBOUND = "otc-inbound"
TOPIC = "parent.orders.v1"
DEALER_TABLES = ("customers", "products", "warehouses", "accounts", "sales_orders",
                 "order_lines", "shipments", "returns")
ERP_DOCTYPES = ("Company", "Customer", "Item", "UOM", "Warehouse", "Account",
                "Sales Order", "Stock Entry", "Delivery Note", "Sales Invoice",
                "Payment Entry", "GL Entry")
ERP_SITE_MASTERS = {"Customer", "Item", "UOM"}


def digest(body):
    return hashlib.sha256(body).hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True).encode("utf-8")


def now():
    return datetime.now(timezone.utc).isoformat()


def s3_client(role):
    return boto3.client(
        "s3", endpoint_url=os.environ[f"{role}_S3_ENDPOINT"],
        aws_access_key_id=os.environ[f"{role}_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ[f"{role}_S3_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, connect_timeout=10, read_timeout=30),
    )


def storage():
    return s3_client("BRONZE")


def exists(client, key):
    try:
        client.head_object(Bucket=BRONZE, Key=key)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        return False
    return True


def create_bucket():
    client = storage()
    s3_client("SOURCE").head_bucket(Bucket=INBOUND)
    try:
        client.head_bucket(Bucket=BRONZE)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        client.create_bucket(Bucket=BRONZE)


def put(client, key, body, content_type="application/json"):
    client.put_object(Bucket=BRONZE, Key=key, Body=body, ContentType=content_type)


def save_snapshot(source, run_id, collections):
    client = storage()
    prefix = f"batch/{source}/{quote(run_id, safe='')}"
    manifest_key = f"{prefix}/manifest.json"
    if exists(client, manifest_key):
        print(f"Already published s3://{BRONZE}/{manifest_key}")
        return

    started = now()
    entries = []
    for name, records in collections:
        body = b"".join(json_bytes(record) + b"\n" for record in records)
        checksum = digest(body)
        key = f"{prefix}/{quote(name, safe='')}-{checksum}.jsonl"
        put(client, key, body, "application/x-ndjson")
        entries.append({"collection": name, "key": key, "rows": len(records), "sha256": checksum})
    put(client, manifest_key, json_bytes({"source": source, "run_id": run_id,
                                         "started_at": started, "published_at": now(), "files": entries}))
    print(f"s3://{BRONZE}/{manifest_key} ({sum(item['rows'] for item in entries)} rows)")


def get_json(base, path, params=None, headers=None):
    url = base + path + ("?" + urlencode(params) if params else "")
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def erp_collections():
    base = os.environ["ERP_INTERNAL_URL"]
    headers = {"Authorization": f"token {os.environ['ERP_KEY']}:{os.environ['ERP_SECRET']}"}
    company = os.environ["ERP_COMPANY"]
    for doctype in ERP_DOCTYPES:
        path = f"/api/resource/{quote(doctype, safe='')}"
        if doctype == "Company":
            yield doctype, [get_json(base, f"{path}/{quote(company, safe='')}", headers=headers)["data"]]
            continue

        # Customer, Item and UOM are site-wide masters on this dedicated demo site.
        filters = [] if doctype in ERP_SITE_MASTERS else [["company", "=", company]]
        after = None
        records = []
        while True:
            page_filters = filters + ([["name", ">", after]] if after is not None else [])
            page = get_json(base, path, {"filters": json.dumps(page_filters), "fields": '["name"]',
                                         "order_by": "name asc", "limit_page_length": 100},
                            headers)["data"]
            if not page:
                break
            for row in page:
                name = quote(row["name"], safe="")
                records.append(get_json(base, f"{path}/{name}", headers=headers)["data"])
            if page[-1]["name"] == after:
                raise RuntimeError(f"ERPNext pagination stalled for {doctype}")
            after = page[-1]["name"]
        yield doctype, records


def pos_collections():
    base = os.environ["COMMERCE_INTERNAL_URL"]
    after = ""
    receipts = []
    while True:
        page = get_json(base, "/pos/receipts", {"after": after, "limit": 100})
        if not page:
            break
        receipts.extend(page)
        if page[-1]["receipt_id"] == after:
            raise RuntimeError("POS pagination stalled")
        after = page[-1]["receipt_id"]
    yield "pos_receipts", receipts


def dealer_collections():
    with psycopg.connect(host=os.environ["DEALER_HOST"], dbname="dealer_legacy",
                         user="dealer_reader", password=os.environ["DEALER_PASSWORD"],
                         port=int(os.environ["DEALER_PORT"]),
                         connect_timeout=15, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for table in DEALER_TABLES:
            query = sql.SQL("SELECT * FROM public.{} ORDER BY id").format(sql.Identifier(table))
            yield table, connection.execute(query).fetchall()


def copy_manifests():
    client = storage()
    source = s3_client("SOURCE")
    published = 0
    prefixes = ("parent/finance/", "dealer/finance/", "dealer/exports/", "seed-v1/")
    for prefix in prefixes:
        found = False
        pages = source.get_paginator("list_objects_v2")
        for page in pages.paginate(Bucket=INBOUND, Prefix=prefix):
            for entry in page.get("Contents", []):
                source_key = entry["Key"]
                if not source_key.endswith("/manifest.json"):
                    continue
                found = True
                raw = source.get_object(Bucket=INBOUND, Key=source_key)["Body"].read()
                dest = f"files/{INBOUND}/{source_key.rsplit('/', 1)[0]}/{digest(raw)}"
                if exists(client, f"{dest}/manifest.json"):
                    continue
                for item in json.loads(raw)["files"]:
                    key = item["key"]
                    data = source.get_object(Bucket=INBOUND, Key=key)["Body"].read()
                    if digest(data) != item["sha256"]:
                        raise ValueError(f"Inbound checksum mismatch for {key}")
                    put(client, f"{dest}/objects/{key}", data, "application/octet-stream")
                put(client, f"{dest}/manifest.json", raw)
                published += 1
        if not found:
            raise RuntimeError(f"No inbound manifest found at {prefix}")
    print(f"Published {published} new inbound manifests")


def b64(data):
    return None if data is None else base64.b64encode(data).decode("ascii")


def drain_kafka():
    client = storage()
    generation = os.environ["KAFKA_SOURCE_GENERATION"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", generation):
        raise ValueError("KAFKA_SOURCE_GENERATION must be a short identifier")
    consumer = Consumer({"bootstrap.servers": os.environ["KAFKA_BOOTSTRAP"],
                         "group.id": f"otc-bronze-airflow-{generation}", "enable.auto.commit": False,
                         "auto.offset.reset": "earliest", "allow.auto.create.topics": False,
                         "enable.partition.eof": True})
    offsets = {}
    try:
        metadata = consumer.list_topics(TOPIC, timeout=15).topics.get(TOPIC)
        if metadata is None or metadata.error or not metadata.partitions:
            raise RuntimeError(f"Kafka topic is unavailable: {TOPIC}")
        consumer.subscribe([TOPIC])
        deadline = time.monotonic() + 45
        count = 0
        ended = set()
        while time.monotonic() < deadline and count < 500:
            message = consumer.poll(1)
            if message is None:
                continue
            partition = message.partition()
            if message.error():
                if message.error().code() != KafkaError._PARTITION_EOF:
                    raise KafkaException(message.error())
                ended.add(partition)
                if ended == set(metadata.partitions):
                    break
                continue

            ended.discard(partition)
            offset = message.offset()
            key = f"kafka/{TOPIC}/generation={generation}/partition={partition}/offset={offset}.json"
            if not exists(client, key):
                stamp_type, stamp_ms = message.timestamp()
                put(client, key, json_bytes({"topic": TOPIC, "generation": generation, "partition": partition,
                                            "offset": offset, "timestamp_type": stamp_type,
                                            "timestamp_ms": stamp_ms, "ingested_at": now(),
                                            "key_base64": b64(message.key()),
                                            "value_base64": b64(message.value()),
                                            "headers": [[name, b64(value)]
                                                        for name, value in message.headers() or []]}))
            offsets[partition] = offset + 1
            count += 1
        if not offsets and ended != set(metadata.partitions):
            raise TimeoutError("Kafka consumer did not reach the end of all partitions")
        if offsets:
            committed = consumer.commit(
                offsets=[TopicPartition(TOPIC, partition, offset) for partition, offset in offsets.items()],
                asynchronous=False,
            )
            for result in committed:
                if result.error:
                    raise KafkaException(result.error)
        print(f"Preserved or confirmed {count} Kafka deliveries")
    finally:
        consumer.close()


def preflight():
    base = os.environ["ERP_INTERNAL_URL"]
    headers = {"Authorization": f"token {os.environ['ERP_KEY']}:{os.environ['ERP_SECRET']}"}
    company = quote(os.environ["ERP_COMPANY"], safe="")
    get_json(base, f"/api/resource/Company/{company}", headers=headers)
    print("ERPNext company and API credentials verified")

    get_json(os.environ["COMMERCE_INTERNAL_URL"], "/pos/receipts", {"limit": 1})
    print("Commerce POS verified")

    with closing(dealer_collections()) as collections:
        _, customers = next(collections)
    if not customers:
        raise RuntimeError("Dealer customer table is empty")
    print("Dealer PostgreSQL reader verified")

    bootstrap = os.environ["KAFKA_BOOTSTRAP"]
    consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": "otc-bronze-preflight",
                         "enable.auto.commit": False, "allow.auto.create.topics": False})
    try:
        metadata = consumer.list_topics(TOPIC, timeout=15)
        topic = metadata.topics.get(TOPIC)
        if topic is None or topic.error or not topic.partitions:
            raise RuntimeError(f"Kafka topic is unavailable: {TOPIC}")
        if {f"{broker.host}:{broker.port}" for broker in metadata.brokers.values()} != {bootstrap}:
            raise RuntimeError("The single-node Kafka broker must advertise the external source endpoint")
    finally:
        consumer.close()
    print("Kafka topic and advertised broker address verified")

    create_bucket()
    print("Inbound MinIO access and platform Bronze bucket verified")


if __name__ == "__main__":
    preflight()
