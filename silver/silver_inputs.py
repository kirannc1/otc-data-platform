"""Resolve committed Bronze inputs and record the exact objects used by Silver."""
from base64 import b64decode
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re

from bronze_review import decode_file


BATCH = {
    "erpnext": ("Company", "Customer", "Item", "UOM", "Warehouse", "Account", "Sales Order",
                "Delivery Note", "Stock Entry", "Sales Invoice", "Payment Entry", "GL Entry"),
    "dealer-sql": ("customers", "products", "warehouses", "accounts", "sales_orders",
                   "order_lines", "shipments", "returns"),
    "parent-pos": ("pos_receipts",),
}
FILES = {
    "dealer_export": "dealer/exports/",
    "parent_finance": "parent/finance/",
    "dealer_finance": "dealer/finance/",
    "mdm": "seed-v1/",
}


def load_inputs(bronze, settings):
    """Read each selected payload once and verify its manifest checksum and count."""
    data, keys, selections = {}, {}, {}
    pins = settings.get("manifests", {})
    unknown = set(pins) - BATCH.keys() - FILES.keys()
    if unknown:
        raise ValueError(f"Unknown manifest settings: {sorted(unknown)}")

    def put(name, key, entry=None):
        if name in data:
            raise ValueError(f"Duplicate input collection {name}")
        payload = bronze.read(key)
        if entry is not None and sha256(payload).hexdigest() != entry["sha256"]:
            raise ValueError(f"Bronze checksum mismatch: {key}")
        rows, _ = decode_file(key, payload)
        if rows is None:
            raise ValueError(f"Unsupported Bronze payload: {key}")
        if entry is not None and entry.get("rows") is not None and len(rows) != entry["rows"]:
            raise ValueError(f"Bronze row count mismatch: {key}")
        data[name], keys[name] = rows, key

    def select(source, prefix, time_field=None):
        eligible = sorted(k for k in bronze.objects if k.startswith(prefix) and k.endswith("/manifest.json"))
        if pins.get(source) is not None:
            if pins[source] not in eligible:
                raise ValueError(f"Manifest for {source} unavailable: {pins[source]}")
            eligible = [pins[source]]
        if not eligible:
            raise ValueError(f"No committed Bronze manifest for {source} at {prefix}")
        if len(eligible) > 1 and time_field is None:
            raise ValueError(f"Pin one manifest for {source} in silver_settings.json. Options: {eligible}")
        choices = [(key, json.loads(bronze.read(key))) for key in eligible]
        key, manifest = max(choices, key=lambda item: (item[1].get(time_field, ""), item[0]))
        selections[source] = key
        return key, manifest

    for source, expected in BATCH.items():
        _, manifest = select(source, f"batch/{source}/", "published_at")
        if manifest["source"] != source:
            raise ValueError(f"Unexpected source in {selections[source]}")
        for entry in manifest["files"]:
            if entry["collection"] in expected:
                put((source, entry["collection"]), entry["key"], entry)
        missing = [name for name in expected if (source, name) not in data]
        if missing:
            raise ValueError(f"Missing collections for {source}: {missing}")

    for source, prefix in FILES.items():
        key, manifest = select(source, f"files/otc-inbound/{prefix}",
                               "cutoff" if source == "dealer_export" else None)
        folder = key.rsplit("/", 1)[0]
        for entry in manifest["files"]:
            original = entry["key"]
            if PurePosixPath(original).suffix not in {".csv", ".json"}:
                continue
            name = re.sub(r"-[a-f0-9]{64}$", "", PurePosixPath(original).stem)
            put((source, name), f"{folder}/objects/{original}", entry)

    deliveries = []
    seen_keys = [k for k in sorted(bronze.objects) if k.startswith("kafka/") and k.endswith(".json")]
    kafka = settings.get("kafka_keys")
    if kafka is not None:
        if len(kafka) != len(set(kafka)) or set(kafka) - set(seen_keys):
            raise ValueError("kafka_keys must contain distinct Bronze Kafka delivery keys")
        seen_keys = sorted(kafka)

    for key in seen_keys:
        envelope = json.loads(bronze.read(key))
        try:
            event = json.loads(b64decode(envelope["value_base64"], validate=True))
            if not isinstance(event, dict):
                raise ValueError("Event value must be an object")
        except (ValueError, TypeError, KeyError) as error:
            event = {"decode_error": str(error)}
        deliveries.append({"bronze_key": key, **{name: envelope.get(name) for name in
                           ("topic", "generation", "partition", "offset", "ingested_at")},
                           "event": event})

    return data, keys, selections, deliveries
