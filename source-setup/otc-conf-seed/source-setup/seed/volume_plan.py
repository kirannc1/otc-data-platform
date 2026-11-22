"""Deterministic order-to-cash scenarios and control totals for a demo batch."""
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
import json
import re

COUNT_FIELDS = ("bookings", "shipments", "invoices", "payments", "pos_receipts")
CHANNEL_FIELDS = ("parent_booked_acceptances", "dealer_booked_acceptances", "parent_cancelled_orders")
CONFIG_FIELDS = ("batch_id", "posting_date", "ar_cutoff", "parent", "dealer", "storefront")


def fields(obj, keys, path):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise ValueError(f"{path} must have exactly these keys: {', '.join(keys)}")


def counts(obj, keys, path):
    fields(obj, keys, path)
    for key, value in obj.items():
        if type(value) is not int or value < 0:
            raise ValueError(f"{path}.{key} must be a non-negative integer")


@dataclass(frozen=True)
class Shipment:
    id: str
    quantity: int
    due: date
    billed: bool
    cash: int


@dataclass(frozen=True)
class Order:
    id: str
    customer: str
    sku: str
    rate: int
    stock_cost: int
    quantity: int
    shipments: tuple[Shipment, ...]


@dataclass(frozen=True)
class Plan:
    config: dict
    day: date
    cutoff: date
    config_sha256: str
    parent: tuple[Order, ...]
    dealer: tuple[Order, ...]

    def prefix(self, entity):
        return f"{entity}-{self.config['batch_id']}"

    def summary(self):
        result = {"batch_id": self.config["batch_id"], "posting_date": self.day.isoformat(),
                  "ar_cutoff": self.cutoff.isoformat(), "config_sha256": self.config_sha256}
        for name, orders in (("parent", self.parent), ("dealer", self.dealer)):
            amounts = Counter()
            for order in orders:
                amounts["booked"] += order.rate * order.quantity
                for shipment in order.shipments:
                    if shipment.billed:
                        amounts["billed"] += order.rate * shipment.quantity
                        amounts["cash"] += shipment.cash
            result[name] = {**self.config[name], "booked_inr": amounts["booked"],
                            "billed_inr": amounts["billed"], "cash_inr": amounts["cash"],
                            "open_ar_inr": amounts["billed"] - amounts["cash"]}
        storefront = self.config["storefront"]
        result["parent_kafka_events"] = storefront["parent_booked_acceptances"] + 2 * storefront["parent_cancelled_orders"]
        result["dealer_storefront_acceptances"] = storefront["dealer_booked_acceptances"]
        result["parent_stock_receipts"] = sum(bool(order.shipments) for order in self.parent)
        return result


def make_orders(batch, entity, day, config):
    booking_count = config["bookings"]
    shipment_count = config["shipments"]
    if not booking_count:
        return ()
    per_order, extra = divmod(shipment_count, booking_count)
    products = (("P-C1", "P-PUMP", 10_000, 6_000), ("P-C2", "P-VALVE", 7_500, 4_000)) if entity == "P" else (
        ("D-C1", "D-PUMP", 9_000, 0),
    )
    orders = []
    position = 0
    for index in range(booking_count):
        customer, sku, rate, stock_cost = products[index % len(products)]
        order_id = f"{entity}-{batch}-O{index + 1:06d}"
        ship_plan = []
        for line in range(per_order + (index < extra)):
            position += 1
            qty = 1 + (position % 3)
            due = day + timedelta(days=(14, 30, 45)[(position - 1) % 3])
            billed = position <= config["invoices"]
            cash = qty * rate // 2 if position <= config["payments"] else 0
            ship_plan.append(Shipment(f"{order_id}-S{line + 1:06d}", qty, due, billed, cash))
        order_qty = sum(shipment.quantity for shipment in ship_plan) or 1 + index % 3
        orders.append(Order(order_id, customer, sku, rate, stock_cost, order_qty, tuple(ship_plan)))
    return tuple(orders)


def load_plan(path):
    content = path.read_bytes()
    config = json.loads(content.decode("utf-8-sig"))
    fields(config, CONFIG_FIELDS, "Config")
    batch = config["batch_id"]
    if not isinstance(batch, str) or not re.fullmatch(r"VOL[A-Z0-9]{1,16}", batch):
        raise ValueError("batch_id must start with VOL, followed by 1–16 uppercase letters or digits")
    day = date.fromisoformat(config["posting_date"])
    cutoff = date.fromisoformat(config["ar_cutoff"])
    if day > date.today() or cutoff < day:
        raise ValueError("posting_date must be today or earlier and ar_cutoff must be on or after posting_date")
    for entity in ("parent", "dealer"):
        group = config[entity]
        counts(group, COUNT_FIELDS, entity)
        bookings, shipments, invoices, payments = (group[name] for name in COUNT_FIELDS[:4])
        if (shipments and not bookings) or invoices > shipments or payments > invoices:
            raise ValueError(f"{entity}: shipments require bookings, invoices cannot exceed shipments, and payments cannot exceed invoices")
    storefront = config["storefront"]
    counts(storefront, CHANNEL_FIELDS, "storefront")
    for entity in ("parent", "dealer"):
        if storefront[f"{entity}_booked_acceptances"] > config[entity]["bookings"]:
            raise ValueError(f"{entity}: booked storefront acceptances cannot exceed bookings")
    return Plan(config, day, cutoff, sha256(content).hexdigest(), make_orders(batch, "P", day, config["parent"]),
                make_orders(batch, "D", day, config["dealer"]))
