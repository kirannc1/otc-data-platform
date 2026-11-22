"""Build Gold from a pinned Silver Delta release or legacy JSONL snapshot."""
import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

UNKNOWN = "__unknown__"
ZERO = Decimal("0")
STAGES = ("bookings", "finance", "products")
VERSION = "1"


def encode(value):
    return json.dumps(value, default=str, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def digest(body):
    return sha256(body).hexdigest()


def dec(value):
    if value is None:
        return None
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"Non-finite measure: {value}")
    return result


def unique(rows, identity):
    result = {}
    for row in rows:
        key = identity(row)
        if key in result or key is None:
            raise ValueError(f"Duplicate or missing source key: {key!r}")
        result[key] = row
    return result


def minio_connection(layer):
    import boto3
    from botocore.config import Config
    from dotenv import dotenv_values

    env_file = Path(__file__).resolve().parent.parent / "ingestion-airflow" / "platform.env"
    env = dotenv_values(env_file, interpolate=False, encoding="utf-8-sig") if env_file.exists() else {}
    endpoint = os.environ.get(f"{layer}_S3_ENDPOINT") or os.environ.get("SILVER_S3_ENDPOINT", "http://127.0.0.1:19000")
    access = os.environ.get(f"{layer}_S3_ACCESS_KEY") or os.environ.get("BRONZE_S3_ACCESS_KEY") or env.get("BRONZE_S3_ACCESS_KEY")
    secret = os.environ.get(f"{layer}_S3_SECRET_KEY") or os.environ.get("BRONZE_S3_SECRET_KEY") or env.get("BRONZE_S3_SECRET_KEY")
    if not access or not secret:
        raise ValueError(f"Set {layer}_S3_ACCESS_KEY and {layer}_S3_SECRET_KEY or configure the local platform.env")
    client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access, aws_secret_access_key=secret,
                          region_name="us-east-1", config=Config(s3={"addressing_style": "path"},
                          connect_timeout=10, read_timeout=30))
    storage_options = {
        "AWS_ENDPOINT_URL": endpoint,
        "AWS_ACCESS_KEY_ID": access,
        "AWS_SECRET_ACCESS_KEY": secret,
        "AWS_REGION": "us-east-1",
        "allow_http": str(endpoint.startswith("http://")).lower(),
        "aws_virtual_hosted_style_request": "false",
        "aws_conditional_put": "etag",
    }
    return client, storage_options


def silver_reader(location, client=None, storage_options=None):
    """Return the manifest, table root, object reader and Delta credentials."""
    location = str(location)
    if not location.startswith("s3://"):
        path = Path(location).resolve()
        if path.is_dir():
            path /= "manifest.json"
        return (path.read_bytes(), path.parent.as_posix(),
                lambda name: (path.parent / name).read_bytes(), None)
    bucket, _, object_key = location[5:].partition("/")
    parent, _, filename = object_key.rpartition("/")
    if not bucket or not parent or filename != "manifest.json":
        raise ValueError("Use the exact Silver manifest URI printed by notebook 10")
    if client is None:
        client, configured_options = minio_connection("SILVER")
        if storage_options is None:
            storage_options = configured_options

    def read(name):
        response = client.get_object(Bucket=bucket, Key=f"{parent}/{name}")
        with response["Body"] as body:
            return body.read()

    return read(filename), f"s3://{bucket}/{parent}", read, storage_options


def manifest_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8")


def read_delta_tables(root, manifest, storage_options):
    from deltalake import DeltaTable

    tables = {}
    for name, spec in manifest["tables"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or spec["path"] != f"tables/{name}":
            raise ValueError(f"Invalid Silver Delta table path: {spec['path']}")
        version = spec["version"]
        if type(version) is not int or version < 0:
            raise ValueError(f"Invalid Silver Delta version for {name}: {version}")
        table = DeltaTable(f"{root}/{spec['path']}", version=version,
                           storage_options=storage_options).to_pyarrow_table()
        rows = table.to_pylist()
        checksum = sha256()
        for encoded in sorted(manifest_bytes(row) for row in rows):
            checksum.update(encoded)
        if len(rows) != spec["rows"] or checksum.hexdigest() != spec["rows_sha256"]:
            raise ValueError(f"Silver Delta checksum or count mismatch: {name}@{version}")
        # Preserve the Gold rules' ISO date interface and exact decimal values.
        tables[name] = [
            {key: value.isoformat() if isinstance(value, date) else value for key, value in row.items()}
            for row in rows
        ]
    return tables


def read_silver(path, *, client=None, storage_options=None):
    manifest_body, root, read_bytes, storage_options = silver_reader(path, client, storage_options)
    manifest = json.loads(manifest_body)
    if manifest.get("format_version") not in {1, 2} or "model_run_id" not in manifest:
        raise ValueError("Expected a published Silver Delta or legacy JSONL manifest")
    provenance = {k: v for k, v in manifest.items() if k != "model_run_id"}
    if digest(manifest_bytes(provenance)) != manifest["model_run_id"]:
        raise ValueError("Silver manifest does not match its model_run_id")
    if manifest["format_version"] == 2:
        if manifest.get("table_format") != "delta" or manifest.get("table_schema_version") != 1:
            raise ValueError("Unsupported Silver Delta manifest schema")
        tables = read_delta_tables(root, manifest, storage_options)
    else:
        tables = {}
        for name, info in manifest["files"].items():
            if Path(name).name != name or "/" in name or "\\" in name or not name.endswith(".jsonl"):
                raise ValueError(f"Invalid Silver filename: {name}")
            body = read_bytes(name)
            rows = [json.loads(line) for line in body.splitlines()]
            if digest(body) != info["sha256"] or len(rows) != info["rows"]:
                raise ValueError(f"Silver checksum or count mismatch: {name}")
            tables[Path(name).stem] = rows
    if any(r["status"] == "FAIL" for r in tables["reconciliation_control"]):
        raise ValueError("Silver reconciliation failed")
    if any(r["severity"] == "error" for r in tables["quality_exception"]):
        raise ValueError("Silver contains error-level exceptions")
    return manifest, tables


class Gold:
    def __init__(self, manifest, silver, settings):
        self.manifest, self.silver, self.settings = manifest, silver, settings
        self.tables = {}
        self.checks = []
        self.exceptions = []

    def source(self, name):
        return self.silver.get(name, [])

    def index(self, name):
        return unique(self.source(name), lambda row: row["id"])

    def check(self, rule, expected, actual):
        self.checks.append(dict(rule=rule, expected=expected, actual=actual, passed=expected == actual))

    def note(self, rule, identity):
        self.exceptions.append(dict(rule=rule, source_id=identity))

    def dimension(self, dimension_name, identity, **attrs):
        identity = identity if identity not in ("", None) else UNKNOWN
        table = self.tables.setdefault(f"dim_{dimension_name}", {})
        member = table.setdefault(identity, {"key": f"{dimension_name}:{identity}", "business_id": identity})
        for field, value in attrs.items():
            if value is not None:
                if field in member and member[field] != value:
                    raise ValueError(f"Conflicting attributes for {dimension_name} {identity}: {field}")
                member[field] = value
        return member["key"]

    def roles(self, **roles):
        output = {}
        for role, (dimension, identity) in roles.items():
            output[f"{role}_key"] = self.dimension(dimension, identity)
        return output

    def customer(self, source_id, on_date, mappings):
        return self.mapped(source_id, on_date, mappings, "customer")

    def mapped(self, source_id, on_date, mappings, name):
        if not source_id or not on_date:
            return None
        matches = [r for r in mappings.get(source_id, []) if r["valid_from"] <= on_date
                   and (not r.get("valid_to") or on_date < r["valid_to"])]
        if len(matches) > 1:
            raise ValueError(f"Overlapping {name} crosswalk for {source_id} on {on_date}")
        return matches[0]["group_id"] if matches else None

    def crosswalk(self, name):
        result = defaultdict(list)
        for row in self.source(f"{name}_crosswalk"):
            result[row["source_id"]].append(row)
        return result

    def store(self, name, rows):
        self.tables[name] = unique(rows, lambda r: r["id"])

    def records(self, name):
        return list(self.tables[name].values())


def build_bookings(gold):
    for row in gold.source("legal_entity"):
        gold.dimension("legal_entity", row["id"], name=row.get("name"))
    for name in ("customer", "product"):
        for row in gold.source(f"group_{name}"):
            gold.dimension(name, row["id"])
    headers, lines, mapped = (gold.index(name) for name in
                              ("sales_order", "sales_order_line", "order_line_conformed"))
    bookings, eligible_amounts = [], []
    for line in lines.values():
        order = headers.get(line.get("sales_order_id"))
        if not order:
            gold.note("booking_header_missing", line["id"])
            continue
        entity, status = order["legal_entity_id"], order.get("status_code")
        parent = entity == "parent" and order.get("docstatus") == 1 and status not in {"cancelled", "canceled"}
        dealer = entity == "dealer" and (status in {"accepted", "completed", "booked"}
            or (status is None and gold.settings["allow_blank_dealer_booking_status"]))
        if not (parent or dealer):
            gold.note("booking_not_eligible", line["id"])
            continue
        day, currency = order.get("order_date"), order.get("currency")
        amount = dec(line["amount"])
        if entity != line["legal_entity_id"]:
            raise ValueError(f"Booking line and header legal entity mismatch: {line['id']}")
        if not day or not currency or amount is None:
            raise ValueError(f"Missing booking date, currency or amount for {line['id']}")
        date.fromisoformat(day)
        conformed = mapped.get(line["id"], {})
        if conformed and conformed["sales_order_id"] != order["id"]:
            raise ValueError(f"Conformed booking belongs to another order: {line['id']}")
        eligible_amounts.append(amount)
        bookings.append(dict(id=line["id"], sales_order_id=order["id"],
                             source_system="erpnext" if parent else "dealer-sql",
                             order_id=order["source_id"], order_line_id=line["source_id"],
                             order_reference=order.get("order_reference"), legal_entity_id=entity,
                             booking_date=day, currency=currency, booked_amount=amount,
                             source_qty=dec(line.get("quantity")), source_uom=line.get("uom"),
                             booked_qty=dec(conformed.get("conformed_quantity")),
                             booked_uom=conformed.get("conformed_uom"), line_count=1,
                             **gold.roles(booking_date=("date", day), customer=("customer", conformed.get("group_customer_id")),
                                          product=("product", conformed.get("group_product_id")),
                                          legal_entity=("legal_entity", entity), currency=("currency", currency))))
    gold.store("fact_sales_order_booking", bookings)
    gold.check("bookings_preserve_eligible_line_count", len(eligible_amounts), len(bookings))
    gold.check("bookings_preserve_eligible_line_amount", sum(eligible_amounts, ZERO),
               sum((r["booked_amount"] for r in bookings), ZERO))
    if gold.settings["check_baseline"]:
        baseline = [r for r in bookings if (r["legal_entity_id"], r["order_reference"])
                    in {("parent", "P-S1"), ("dealer", "D-S1")}]
        gold.check("baseline_booking_rows", 2, len(baseline))
        gold.check("baseline_distinct_orders", 2, len({r["sales_order_id"] for r in baseline}))
        gold.check("baseline_booking_inr", Decimal(140000),
                   sum((r["booked_amount"] for r in baseline if r["currency"] == "INR"), ZERO))
        gold.check("baseline_shared_customer_product", 1,
                   len({(r["customer_key"], r["product_key"]) for r in baseline
                        if UNKNOWN not in (r["customer_key"].split(":", 1)[1], r["product_key"].split(":", 1)[1])}))
        gold.check("baseline_pumps_each", Decimal(11),
                   sum((r["booked_qty"] for r in baseline if r["booked_qty"] is not None
                        and r["booked_uom"] == "each"), ZERO))

    shipments = gold.index("shipment")
    customer_map, product_map = gold.crosswalk("customer"), gold.crosswalk("product")
    output = []
    for line in gold.source("shipment_line"):
        header = shipments.get(line["document_id"])
        if not header or (header.get("docstatus") != 1 and line["legal_entity_id"] != "dealer"):
            gold.note("shipment_header_missing_or_unposted", line["id"])
            continue
        entity, day = line["legal_entity_id"], header.get("posting_date")
        related_line = lines.get(line.get("sales_order_line_id"), {})
        order = headers.get(related_line.get("sales_order_id") or header.get("sales_order_id"), {})
        order_day = order.get("order_date")
        # The order date can establish mapping context without replacing the shipment date.
        mapping_day = day or order_day
        output.append(dict(id=line["id"], shipment_id=header["id"], shipment_line_id=line["source_id"],
                           sales_order_line_id=line.get("sales_order_line_id"),
                           shipped_qty=dec(line.get("quantity")), shipped_uom=line.get("uom"), line_count=1,
                           **gold.roles(shipment_date=("date", day), legal_entity=("legal_entity", entity),
                               warehouse=("warehouse", line.get("warehouse_id")),
                               customer=("customer", gold.customer(order.get("source_customer_id"), mapping_day, customer_map)),
                               product=("product", gold.mapped(line.get("source_product_id"), mapping_day, product_map, "product")))))
    gold.store("fact_shipment_line", output)


def build_finance(gold):
    customer_map, account_map = gold.crosswalk("customer"), gold.crosswalk("account")
    cutoff = gold.settings["cutoff"]
    billings, postings, cash, snapshots = [], [], [], []
    docs, payments = gold.index("billing_document"), gold.index("payment")
    for row in docs.values():
        if row["docstatus"] != 1 or not row["posting_date"] or row["posting_date"] > cutoff:
            continue
        if dec(row["signed_total"]) is None:
            raise ValueError(f"Posted document amount missing: {row['id']}")
        entity, day = row["legal_entity_id"], row["posting_date"]
        billings.append(dict(id=row["id"], legal_entity_id=entity, document_id=row["source_id"],
            document_reference=row.get("source_reference"), original_invoice_id=row.get("original_invoice_id"),
            sales_order_id=row.get("sales_order_id"), document_type=row["document_type"],
            posting_date=day, due_date=row.get("due_date"), currency=row["currency"],
            signed_billed_amount=dec(row["signed_total"]), document_count=1,
            **gold.roles(document_date=("date", day), invoice_due_date=("date", row.get("due_date")),
                customer=("customer", gold.customer(row.get("source_customer_id"), day, customer_map)),
                legal_entity=("legal_entity", entity), document_type=("document_type", row["document_type"]),
                currency=("currency", row["currency"]))))
    gold.store("fact_billing_document", billings)
    billing = gold.tables["fact_billing_document"]
    for row in gold.source("gl_account"):
        gold.dimension("gl_account", row["id"], name=row.get("name"))
    for row in gold.source("gl_posting_line"):
        day, entity = row.get("posting_date"), row["legal_entity_id"]
        if row["is_cancelled"] or not day or day > cutoff:
            continue
        account = row.get("gl_account_id")
        category = gold.mapped(account, day, account_map, "account")
        debit, credit = dec(row["debit"]), dec(row["credit"])
        if debit is None or credit is None:
            raise ValueError(f"Missing debit or credit: {row['id']}")
        postings.append(dict(id=row["id"], legal_entity_id=entity, posting_date=day,
            currency=row["company_currency"], gl_entry_id=row["source_id"], voucher_id=row.get("voucher_source_id"),
            voucher_type=row.get("voucher_type"), billing_document_id=row.get("billing_document_id"),
            account_source_id=account, reporting_category=category, debit_amount=debit, credit_amount=credit,
            gl_line_count=1, **gold.roles(posting_date=("date", day), legal_entity=("legal_entity", entity),
                gl_account=("gl_account", account), account_category=("reporting_account_category", category),
                currency=("currency", row["company_currency"]))))
    gold.store("fact_gl_posting", postings)

    for row in gold.source("payment_allocation"):
        invoice, payment = billing.get(row.get("billing_document_id")), payments.get(row.get("payment_id"))
        if (not invoice or not payment or payment.get("docstatus") != 1 or invoice["document_type"] != "invoice"
                or payment.get("currency") != invoice["currency"]
                or payment.get("legal_entity_id") != invoice["legal_entity_id"]
                or payment.get("payment_type") != "Receive" or dec(row.get("allocated_amount")) is None):
            gold.note("allocation_missing_posted_links_currency_or_amount", row["id"])
            continue
        # The source contains a payment date but has no independent application timestamp.
        if payment.get("posting_date") and payment["posting_date"] > cutoff:
            continue
        cash.append(dict(id=row["id"], payment_id=payment["id"], invoice_id=invoice["id"],
            allocation_id=row["id"], legal_entity_id=invoice["legal_entity_id"],
            payment_date=payment.get("posting_date"), application_date=None, invoice_due_date=invoice["due_date"],
            allocated_cash_amount=dec(row["allocated_amount"]), allocation_count=1, currency=invoice["currency"],
            customer_key=invoice["customer_key"],
            **gold.roles(payment_date=("date", payment.get("posting_date")), application_date=("date", None),
                invoice_due_date=("date", invoice["due_date"]), legal_entity=("legal_entity", invoice["legal_entity_id"]),
                currency=("currency", invoice["currency"]))))
    gold.store("fact_payment_allocation", cash)

    for name in ("calculated_ar_balance", "receivables_snapshot"):
        index = unique((r for r in gold.source(name) if r["cutoff"] == cutoff and r.get("billing_document_id")),
                       lambda r: (r["billing_document_id"], r["cutoff"], r["currency"]))
        if name == "calculated_ar_balance":
            calculated = index
        else:
            observed = index
    for index in sorted(calculated.keys() | observed.keys()):
        invoice_id, day, currency = index
        invoice = billing.get(invoice_id)
        if not invoice or invoice["currency"] != currency or invoice["document_type"] != "invoice":
            gold.note("ar_missing_invoice_or_currency", invoice_id)
            continue
        computed = dec(calculated.get(index, {}).get("open_amount"))
        reported = dec(observed.get(index, {}).get("open_amount"))
        if computed is not None and reported is not None:
            gold.check(f"ar_computed_matches_reported:{invoice_id}", computed, reported)
        selected = computed if computed is not None else reported
        if selected is None:
            gold.note("ar_missing_balance_at_cutoff", invoice_id)
            continue
        if selected <= ZERO:
            continue
        due = invoice["due_date"]
        days = (date.fromisoformat(day) - date.fromisoformat(due)).days if due else None
        bucket = None if days is None else ("Current" if days <= 0 else "1–30" if days <= 30 else
                  "31–60" if days <= 60 else "61–90" if days <= 90 else "91+")
        snapshots.append(dict(id=encode(index), invoice_id=invoice_id, legal_entity_id=invoice["legal_entity_id"],
            as_of_date=day, invoice_date=invoice["posting_date"], due_date=due, customer_key=invoice["customer_key"],
            currency=currency, open_amount=selected, days_past_due=days, aging_bucket=bucket,
            balance_basis="calculated" if computed is not None else "reported",
            coverage_status="matched" if computed is not None and reported is not None else
                            "calculated_only" if computed is not None else "reported_only",
            **gold.roles(as_of_date=("date", day), invoice_date=("date", invoice["posting_date"]),
                invoice_due_date=("date", due), legal_entity=("legal_entity", invoice["legal_entity_id"]),
                currency=("currency", currency), aging_bucket=("aging_bucket", bucket))))
    gold.store("fact_ar_snapshot", snapshots)


def build_planning_events(gold):
    budgets, events = [], []
    for row in gold.source("budget_amount"):
        scope = encode([row["legal_entity_id"], row["scope_type"], row["scope"]])
        # Fiscal calendar is a separate approved mapping. The fixture publishes a calendar month.
        fiscal = row["period"] if gold.settings["calendar_month_is_fiscal_period"] else None
        budgets.append(dict(id=row["id"], legal_entity_id=row["legal_entity_id"], period=row["period"],
            fiscal_period=fiscal, scenario=row["scenario"], budget_version=row["input_version"],
            scope_type=row["scope_type"], scope=row["scope"], currency=row["currency"],
            budget_amount=dec(row["amount"]), **gold.roles(fiscal_period=("fiscal_period", fiscal),
                legal_entity=("legal_entity", row["legal_entity_id"]), currency=("currency", row["currency"]),
                scenario=("budget_scenario", row["scenario"]), budget_version=("budget_version", row["input_version"]),
                planning_scope=("planning_scope", scope))))
    gold.store("fact_budget", budgets)
    for row in gold.source("order_event"):
        try:
            stamp = datetime.fromisoformat(row["event_time"]) if row.get("event_time") else None
        except ValueError:
            stamp = None
        if stamp is None or stamp.tzinfo is None:
            gold.note("event_missing_aware_timestamp", row["id"])
            continue
        stamp = stamp.astimezone(timezone.utc)
        status = row.get("status_code")
        if status in {"canceled", "cancelled"}:
            status = "cancelled"
        identity = row.get("sales_order_id") or (encode([row["legal_entity_id"], row["order_reference"]])
                                                   if row.get("order_reference") else None)
        channel = gold.settings["channel_by_entity"].get(row["legal_entity_id"])
        events.append(dict(id=row["id"], legal_entity_id=row["legal_entity_id"], logical_event_id=row["source_id"],
            scoped_order_id=identity, event_timestamp_utc=stamp, event_type=status, channel=channel, event_count=1,
            **gold.roles(event_date=("date", stamp.date().isoformat()), event_time=("time", stamp.strftime("%H:%M:%S")),
                event_type=("order_event_type", status), channel=("channel", channel),
                legal_entity=("legal_entity", row["legal_entity_id"]))))
    gold.store("fact_order_event", events)


def build_metrics(gold):
    bookings, bills, posts, cash, ar, events, plans = (gold.records(name) for name in (
        "fact_sales_order_booking", "fact_billing_document", "fact_gl_posting", "fact_payment_allocation",
        "fact_ar_snapshot", "fact_order_event", "fact_budget"))
    cutoff = gold.settings["cutoff"]
    sales = defaultdict(lambda: dict(booked_amount=ZERO, net_billed_amount=ZERO, orders=set()))
    customers = defaultdict(lambda: dict(net_billed_amount=ZERO, orders={}, booked_amount=ZERO))
    for row in bookings:
        if row["booking_date"] > cutoff:
            continue
        bucket = sales[row["booking_date"], row["legal_entity_id"], row["currency"]]
        bucket["booked_amount"] += row["booked_amount"]
        bucket["orders"].add(row["sales_order_id"])
        customer = customers[row["customer_key"], row["currency"]]
        customer["orders"][row["sales_order_id"]] = row["booking_date"]
        customer["booked_amount"] += row["booked_amount"]
    for row in bills:
        sales[row["posting_date"], row["legal_entity_id"], row["currency"]]["net_billed_amount"] += row["signed_billed_amount"]
        customers[row["customer_key"], row["currency"]]["net_billed_amount"] += row["signed_billed_amount"]
    gold.store("gold_sales_daily", [
        dict(id=encode([day, entity, currency]), day=day, legal_entity_id=entity, currency=currency,
             booked_amount=r["booked_amount"], net_billed_amount=r["net_billed_amount"],
             distinct_orders=len(r["orders"]), gross_margin=None, coverage="entity totals; region, category and margin deferred")
        for (day, entity, currency), r in sorted(sales.items())])
    customer_output = []
    for (customer, currency), row in sorted(customers.items()):
        latest = max(row["orders"].values(), default=None)
        complete_history = gold.settings["history_complete"]
        inactive = None
        if latest and complete_history and customer != f"customer:{UNKNOWN}":
            inactive = (date.fromisoformat(cutoff) - date.fromisoformat(latest)).days >= 120
        customer_output.append(dict(id=encode([customer, currency, cutoff]), customer_key=customer, currency=currency,
            as_of_date=cutoff, observed_net_billed_amount=row["net_billed_amount"],
            lifetime_net_billed_amount=row["net_billed_amount"] if complete_history and customer != f"customer:{UNKNOWN}" else None,
            last_observed_order_date=latest, observed_distinct_orders=len(row["orders"]), inactive_120_days=inactive,
            coverage="complete history declared" if complete_history else "observed history only"))
    gold.store("gold_customer_360", customer_output)
    gold.store("gold_ar_aging", ar)

    live = defaultdict(lambda: dict(orders=set(), cancellations=0))
    for row in events:
        stamp = row["event_timestamp_utc"]
        window = stamp.replace(minute=stamp.minute // 5 * 5, second=0, microsecond=0)
        if row["event_type"] == "accepted" and row["scoped_order_id"]:
            live[window, row["channel"] or UNKNOWN]["orders"].add(row["scoped_order_id"])
        elif row["event_type"] == "accepted":
            gold.note("accepted_event_without_order_identity", row["id"])
        elif row["event_type"] == "cancelled":
            live[window, row["channel"] or UNKNOWN]["cancellations"] += 1
    gold.store("gold_orders_live", [
        dict(id=encode([stamp, channel]), window_start_utc=stamp, channel=channel,
             accepted_distinct_orders=len(row["orders"]), cancellation_events=row["cancellations"])
        for (stamp, channel), row in sorted(live.items())])

    revenue, unlinked = defaultdict(lambda: ZERO), []
    for row in posts:
        if row["reporting_category"] == "SALES_REVENUE":
            linked = row.get("billing_document_id")
            if not linked or linked not in gold.tables["fact_billing_document"]:
                unlinked.append(row)
                continue
            revenue[linked, row["currency"]] += row["credit_amount"] - row["debit_amount"]
    rec = []
    for row in bills:
        actual = revenue.get((row["id"], row["currency"]))
        expected = row["signed_billed_amount"]
        diff = expected - actual if actual is not None else None
        rec.append(dict(id=encode([row["id"], cutoff]), billing_document_id=row["id"],
                        legal_entity_id=row["legal_entity_id"], currency=row["currency"], as_of_date=cutoff,
                        signed_billed_amount=expected, posted_revenue=actual, difference=diff,
                        status="unmatched_or_uncovered" if diff is None else "matched" if diff == ZERO else "difference",
                        amount_basis="fixture signed total; no tax"))
    gold.store("gold_revenue_reconciliation", rec)
    gold.store("gold_revenue_unlinked_postings", unlinked)
    for row in rec:
        if row["status"] != "matched":
            gold.note(f"revenue_{row['status']}", row["billing_document_id"])
    for row in unlinked:
        gold.note("revenue_posting_without_linked_document", row["id"])

    if gold.settings["check_baseline"]:
        base_orders = {r["sales_order_id"] for r in bookings if r["order_reference"] in {"P-S1", "D-S1"}}
        base_docs = {r["id"] for r in bills if r["sales_order_id"] in base_orders}
        base_docs |= {r["id"] for r in bills if r["original_invoice_id"] in base_docs}
        sample = [r for r in rec if r["billing_document_id"] in base_docs and r["currency"] == "INR"]
        gold.check("baseline_net_billing_inr", Decimal(135000),
                   sum((r["signed_billed_amount"] for r in sample), ZERO))
        gold.check("baseline_gl_net_revenue_inr", Decimal(135000),
                   sum((r["posted_revenue"] for r in sample if r["posted_revenue"] is not None), ZERO))
        gold.check("baseline_reconciliation_is_matched", bool(sample),
                   bool(sample) and all(r["status"] == "matched" for r in sample))
        if cutoff == "2026-12-10":
            gold.check("baseline_open_ar_inr", Decimal(100000),
                       sum((r["open_amount"] for r in ar if r["invoice_id"] in base_docs and r["currency"] == "INR"), ZERO))

    build_collections_and_budget(gold, bills, posts, cash, plans)


def build_collections_and_budget(gold, bills, posts, cash, plans):
    cutoff = gold.settings["cutoff"]
    cohort = defaultdict(lambda: dict(invoices=0, gross=ZERO, cash=ZERO, net_due=ZERO,
                                     ratio_covered=True))
    credits = defaultdict(lambda: ZERO)
    ambiguous_credit_entities = set()
    for row in bills:
        if row["document_type"] != "credit_note":
            continue
        original = gold.tables["fact_billing_document"].get(row["original_invoice_id"])
        if (original and original["document_type"] == "invoice"
                and (original["legal_entity_id"], original["currency"]) == (row["legal_entity_id"], row["currency"])):
            credits[original["id"]] += row["signed_billed_amount"]
        else:
            ambiguous_credit_entities.add((row["legal_entity_id"], row["currency"]))
    applications = defaultdict(list)
    for row in cash:
        applications[row["invoice_id"]].append(row)
    for invoice in bills:
        if invoice["document_type"] != "invoice":
            continue
        entity, currency, due = invoice["legal_entity_id"], invoice["currency"], invoice["due_date"]
        group = cohort[due[:7] if due else None, entity, currency]
        group["invoices"] += 1
        group["gross"] += invoice["signed_billed_amount"]
        group["ratio_covered"] &= (entity, currency) not in ambiguous_credit_entities and bool(due)
        group["net_due"] += invoice["signed_billed_amount"] + credits[invoice["id"]]
        for row in applications[invoice["id"]]:
            if row["payment_date"] and row["payment_date"] <= cutoff:
                group["cash"] += row["allocated_cash_amount"]
    collections = []
    for (month, entity, currency), row in cohort.items():
        # Payment posting basis supports an observed amount. Settlement dates and cutoff rate
        # need true application times under a separately approved policy.
        collections.append(dict(id=encode([month, entity, currency, cutoff]), due_month=month,
            legal_entity_id=entity, currency=currency, as_of_date=cutoff, invoices=row["invoices"],
            gross_due_amount=row["gross"], eligible_due_amount=row["net_due"] if row["ratio_covered"] else None,
            cash_by_payment_posting_date=row["cash"], collection_rate=None, avg_days_to_pay=None,
            coverage="application timestamps unavailable" if row["ratio_covered"] else "due date or credit linkage unresolved"))
    gold.store("gold_collections_monthly", sorted(collections, key=lambda row: row["id"]))

    actual = defaultdict(lambda: ZERO)
    for row in posts:
        actual[row["legal_entity_id"], row["posting_date"][:7], row["currency"],
               row["account_source_id"]] += row["credit_amount"] - row["debit_amount"]
    budget = []
    for row in plans:
        account_id = json.dumps([row["legal_entity_id"], row["scope"]], separators=(",", ":"))
        scope = (row["legal_entity_id"], row["period"], row["currency"], account_id)
        aligned = row["scope_type"] == "local_account" and row["fiscal_period"] is not None
        # This fixture's dealer plan is a revenue-account plan. Match only that mapped category.
        is_revenue = any(p["account_source_id"] == account_id and p["reporting_category"] == "SALES_REVENUE"
                         and (p["legal_entity_id"], p["posting_date"][:7], p["currency"]) == scope[:3] for p in posts)
        comparable = actual.get(scope) if aligned and is_revenue else None
        budget.append(dict(id=row["id"], legal_entity_id=row["legal_entity_id"], period=row["period"],
            fiscal_period=row["fiscal_period"], scope_type=row["scope_type"], scope=row["scope"], scenario=row["scenario"],
            budget_version=row["budget_version"], currency=row["currency"], budget_amount=row["budget_amount"],
            actual_amount=comparable, variance=comparable - row["budget_amount"]
                if comparable is not None and row["budget_amount"] is not None else None,
            coverage="mapped revenue account through cutoff" if comparable is not None else "actual scope unavailable or fiscal calendar unconfirmed",
            as_of_date=cutoff))
    gold.store("gold_pnl_vs_budget", budget)


def validate(gold):
    for name, rows in gold.tables.items():
        if not name.startswith("fact_"):
            continue
        for row in rows.values():
            for field, value in row.items():
                if not field.endswith("_key"):
                    continue
                dimension, _, business_id = value.partition(":")
                if business_id not in gold.tables.get(f"dim_{dimension}", {}):
                    raise ValueError(f"Broken dimension reference {name}.{field}: {value}")
    if gold.settings["cutoff"] != gold.manifest.get("rules", {}).get("ar_cutoff"):
        raise ValueError("Gold cutoff must match the pinned Silver release AR cutoff. Publish Silver for the desired cutoff.")


SETTINGS = {
    "silver_manifest": "",
    "output_root": "gold_output",
    "gold_bucket": "otc-gold",
    "allow_blank_dealer_booking_status": True,
    "check_baseline": True,
    "history_complete": False,
    "calendar_month_is_fiscal_period": False,
    "channel_by_entity": {},
}


def build(settings_path="gold_settings.json", stage="products", *, silver_client=None, silver_storage_options=None):
    config = {**SETTINGS, **json.loads(Path(settings_path).read_text(encoding="utf-8-sig"))}
    if not config["silver_manifest"]:
        raise ValueError("Set silver_manifest to the exact manifest URI or path printed by notebook 10")
    location = config.pop("silver_manifest")
    if not location.startswith("s3://"):
        location = Path(settings_path).resolve().parent / location
    manifest, tables = read_silver(location, client=silver_client, storage_options=silver_storage_options)
    cutoff = manifest.get("rules", {}).get("ar_cutoff")
    if not cutoff:
        raise ValueError("The Silver manifest must record ar_cutoff")
    date.fromisoformat(cutoff)
    config["cutoff"] = cutoff
    core = {"legal_entity", "sales_order", "sales_order_line", "order_line_conformed"}
    finance = {"billing_document", "gl_posting_line", "payment_allocation", "payment", "calculated_ar_balance"}
    needed = core | (finance if stage in {"finance", "products"} else set())
    missing = needed - tables.keys()
    if missing:
        raise ValueError(f"Published Silver release missing required inputs: {sorted(missing)}")
    gold = Gold(manifest, tables, config)
    gold.stage = stage
    steps = STAGES.index(stage)
    build_bookings(gold)
    if steps >= 1:
        build_finance(gold)
    if steps >= 2:
        build_planning_events(gold)
        build_metrics(gold)
    validate(gold)
    return gold


def publication_rows(gold):
    if gold.stage != "products" or any(not check["passed"] for check in gold.checks):
        raise ValueError("A full Gold build with passing controls is required to publish")
    tables = {name: list(rows.values()) for name, rows in gold.tables.items()}
    tables.update(gold_validation=gold.checks, gold_exceptions=gold.exceptions)
    return tables


def publish(gold, output_root):
    """Export the local JSONL snapshot for existing callers."""
    tables = publication_rows(gold)
    outputs = {f"{name}.jsonl": "".join(encode(row) + "\n" for row in rows).encode()
               for name, rows in tables.items()}
    files = {name: {"rows": body.count(b"\n"), "sha256": digest(body)} for name, body in outputs.items()}
    release = dict(build_version=VERSION, silver_run_id=gold.manifest["model_run_id"],
                   silver_rules=gold.manifest.get("rules", {}),
                   rules={key: value for key, value in gold.settings.items() if key not in {"output_root", "gold_bucket"}}, files=files)
    release_id = digest(encode(release).encode())
    manifest = (encode({"gold_release_id": release_id, **release}) + "\n").encode()
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / release_id

    def verify_target():
        if ((target / "manifest.json").read_bytes() != manifest
                or any(digest((target / name).read_bytes()) != meta["sha256"] for name, meta in files.items())):
            raise ValueError(f"Existing Gold release is inconsistent: {target}")
        return target

    if target.exists():
        return verify_target()
    with TemporaryDirectory(prefix=".gold-", dir=root) as staging:
        payload = Path(staging) / "release"
        payload.mkdir()
        for name, body in outputs.items():
            (payload / name).write_bytes(body)
        (payload / "manifest.json").write_bytes(manifest)
        try:
            payload.rename(target)
        except OSError:
            if not target.exists():
                raise
            return verify_target()
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", default="gold_settings.json")
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("--publish", action="store_true", help="Publish Gold Delta tables to MinIO")
    targets.add_argument("--publish-local", action="store_true", help="Export Gold JSONL to the local output directory")
    args = parser.parse_args()
    gold = build(args.settings)
    print(json.dumps(gold.checks, default=str, indent=2))
    if any(not row["passed"] for row in gold.checks):
        raise SystemExit("Gold controls failed")
    if args.publish:
        from gold_delta import publish_to_minio

        print(publish_to_minio(gold))
    elif args.publish_local:
        print(publish(gold, Path(args.settings).resolve().parent / gold.settings["output_root"]).resolve())


if __name__ == "__main__":
    main()
