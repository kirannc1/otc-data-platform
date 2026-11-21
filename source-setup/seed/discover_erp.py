"""List live ERPNext seed configuration candidates without modifying the site."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import quote

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(__file__).with_name("erp.env"))
    parser.add_argument("--company", help="Exact company ID from the initial discovery")
    args = parser.parse_args()
    settings = {}
    if args.env.is_file():
        for line in args.env.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"ERP_URL", "ERP_KEY", "ERP_SECRET"}:
                settings[key] = value.strip()
    for key in ("ERP_URL", "ERP_KEY", "ERP_SECRET"):
        value = settings.get(key, os.environ.get(key, ""))
        if not value or value.startswith("<"):
            parser.error(f"Set {key} in {args.env}")
        settings[key] = value

    with httpx.Client(base_url=settings["ERP_URL"].rstrip("/"), timeout=20,
                      headers={"Authorization": f"token {settings['ERP_KEY']}:{settings['ERP_SECRET']}"}) as api:
        def get(path, params=None):
            response = api.get(path, params=params)
            if not response.is_success:
                raise RuntimeError(f"ERPNext GET {path} returned HTTP {response.status_code}")
            return response.json()

        def rows(doctype, fields, filters=None):
            records = []
            while True:
                page = get("/api/resource/" + quote(doctype), {
                    "fields": json.dumps(fields), "filters": json.dumps(filters or {}),
                    "limit_start": len(records), "limit_page_length": 100, "order_by": "name asc",
                })["data"]
                records.extend(page)
                if len(page) < 100:
                    return records

        result = {"user": get("/api/method/frappe.auth.get_logged_user")["message"]}
        if result["user"] == "Guest":
            raise RuntimeError("ERPNext returned Guest; check API credentials")
        result["companies"] = rows("Company", ["name", "abbr", "default_currency"])
        if args.company:
            company = get("/api/resource/Company/" + quote(args.company, safe=""))["data"]
            scope = {"company": company["name"]}
            accounts = rows("Account", ["name", "account_type", "root_type", "account_currency"],
                            {**scope, "is_group": 0, "disabled": 0})
            rules = {
                "ERP_BANK": ("default_bank_account", "Bank", None),
                "ERP_AR": ("default_receivable_account", "Receivable", None),
                "ERP_INCOME": ("default_income_account", None, "Income"),
                "ERP_EXPENSE": ("default_expense_account", None, "Expense"),
                "ERP_INVENTORY": ("default_inventory_account", "Stock", None),
            }
            result["company"] = company["name"]
            result["currency"] = company["default_currency"]
            result["account_options"] = {}
            for env, (field, account_type, root_type) in rules.items():
                choices = [a for a in accounts if (not account_type or a["account_type"] == account_type)
                           and (not root_type or a["root_type"] == root_type)]
                result["account_options"][env] = {
                    "configured_company_default": company.get(field), "options": choices,
                }
            for env, doctype, filters in (
                ("ERP_CUSTOMER_GROUP", "Customer Group", {"is_group": 0}),
                ("ERP_TERRITORY", "Territory", {"is_group": 0}),
                ("ERP_ITEM_GROUP", "Item Group", {"is_group": 0}),
                ("ERP_UOM", "UOM", {"enabled": 1}),
                ("ERP_WAREHOUSE_GROUP", "Warehouse", {**scope, "is_group": 1, "disabled": 0}),
                ("ERP_COST_CENTER", "Cost Center", {**scope, "is_group": 0, "disabled": 0}),
            ):
                result[env] = [row["name"] for row in rows(doctype, ["name"], filters)]
            seed_field = "custom_otc_seed_id"
            result["seed_field_readable"] = {}
            for doctype in ("Sales Order", "Stock Entry", "Delivery Note", "Sales Invoice"):
                probe = api.get("/api/resource/" + quote(doctype), params={
                    "filters": json.dumps({seed_field: "OTC-PREFLIGHT"}),
                    "fields": '["name"]', "limit_page_length": 1,
                })
                result["seed_field_readable"][doctype] = probe.status_code
        elif not result["companies"]:
            result["next_step"] = "Complete ERPNext setup or grant the seed user read access to the demo company."
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, httpx.RequestError) as error:
        raise SystemExit(str(error)) from None
