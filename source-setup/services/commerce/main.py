import json
import os
import sys
import time
from datetime import datetime, timezone

import psycopg
from confluent_kafka import Producer
from fastapi import FastAPI, HTTPException, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
from typing import Literal

app = FastAPI()


class Line(BaseModel):
    sku: str
    quantity: int = Field(gt=0)
    unit_price_minor: int = Field(ge=0)


class Event(BaseModel):
    event_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    status: Literal["accepted", "changed", "cancelled"]
    lines: list[Line] = Field(min_length=1)


class Receipt(BaseModel):
    receipt_id: str
    branch: str
    lines: list[Line] = Field(min_length=1)
    tender: Literal["cash", "card", "upi"]


def connect():
    return psycopg.connect(os.environ["COMMERCE_DSN"], connect_timeout=3)


@app.get("/health")
def health():
    with connect() as db:
        db.execute("SELECT 1 FROM orders LIMIT 1")
    return {"ready": True}


@app.post("/orders")
def record_order(event: Event):
    incoming = event.model_dump()
    with connect() as db:
        # Serialize retries of the same event and updates to the same order.
        db.execute("SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", (event.order_id,))
        existing = db.execute(
            "SELECT payload FROM outbox WHERE event_id = %s", (event.event_id,)
        ).fetchone()
        if existing:
            if any(existing[0][k] != v for k, v in incoming.items()):
                raise HTTPException(409, "Event ID already has a different payload")
            return existing[0]
        current = db.execute(
            "SELECT payload FROM orders WHERE order_id=%s", (event.order_id,)
        ).fetchone()
        if (current is None) != (event.status == "accepted") or (
            current and current[0]["status"] == "cancelled"
        ):
            raise HTTPException(409, "Invalid order status transition")
        payload = {
            **incoming, "entity": "parent", "currency": "INR", "schema_version": 1,
            "event_time": datetime.now(timezone.utc).isoformat(),
        }
        inserted = db.execute(
            "INSERT INTO outbox(event_id, payload) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING RETURNING event_id", (event.event_id, Jsonb(payload)),
        ).fetchone()
        if not inserted:
            raise HTTPException(409, "Event ID already belongs to another order")
        db.execute(
            "INSERT INTO orders VALUES (%s,%s) ON CONFLICT (order_id) "
            "DO UPDATE SET payload=excluded.payload",
            (event.order_id, Jsonb(payload)),
        )
    return payload


@app.post("/pos/receipts", status_code=201)
def record_receipt(receipt: Receipt):
    payload = receipt.model_dump()
    with connect() as db:
        if not db.execute(
            "INSERT INTO pos_receipts VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING RETURNING receipt_id",
            (receipt.receipt_id, Jsonb(payload)),
        ).fetchone():
            existing = db.execute(
                "SELECT payload FROM pos_receipts WHERE receipt_id=%s", (receipt.receipt_id,)
            ).fetchone()[0]
            if existing != payload:
                raise HTTPException(409, "Receipt ID already exists")
            return Response(status_code=204)
    return payload


@app.get("/pos/receipts")
def receipts(after: str = "", limit: int = 100):
    with connect() as db:
        rows = db.execute(
            "SELECT payload FROM pos_receipts WHERE receipt_id > %s "
            "ORDER BY receipt_id LIMIT %s", (after, max(1, min(limit, 1000))),
        ).fetchall()
    return [row[0] for row in rows]


def publish():
    producer = Producer({
        "bootstrap.servers": os.environ["KAFKA_BOOTSTRAP"],
        "enable.idempotence": True, "message.timeout.ms": 10000,
    })
    while True:
        try:
            with connect() as db:
                row = db.execute(
                    "SELECT event_id,payload FROM outbox WHERE NOT published "
                    "ORDER BY seq LIMIT 1 FOR UPDATE"
                ).fetchone()
                if row:
                    errors = []
                    producer.produce(
                        "parent.orders.v1", key=row[1]["order_id"],
                        value=json.dumps(row[1]), on_delivery=lambda error, _: errors.append(error),
                    )
                    if producer.flush(12) or errors != [None]:
                        raise RuntimeError("Kafka delivery failed")
                    db.execute("UPDATE outbox SET published=true WHERE event_id=%s", (row[0],))
        except (psycopg.Error, RuntimeError) as error:
            print(f"Outbox retry: {type(error).__name__}", flush=True)
            time.sleep(3)
        else:
            if not row:
                time.sleep(1)


if __name__ == "__main__" and sys.argv[1:] == ["publish"]:
    publish()
