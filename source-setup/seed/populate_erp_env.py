"""Populate ERP seed settings from discover_erp.py."""
import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MASTER_FIELDS = (
    "ERP_CUSTOMER_GROUP", "ERP_TERRITORY", "ERP_ITEM_GROUP", "ERP_UOM",
    "ERP_WAREHOUSE_GROUP", "ERP_COST_CENTER",
)
ACCOUNTS = ("ERP_BANK", "ERP_AR", "ERP_INCOME", "ERP_EXPENSE", "ERP_INVENTORY")
DOCTYPES = ("Sales Order", "Stock Entry", "Delivery Note", "Sales Invoice")


def select(key, options, suggested=None):
    if len(options) == 1:
        print(f"{key}: {options[0]}")
        return options[0]
    print(f"\n{key}")
    for index, option in enumerate(options, 1):
        marker = " (company default)" if option == suggested else ""
        print(f"  {index}. {option}{marker}")
    while True:
        answer = input("Choose a number: ").strip()
        if answer.isascii() and answer.isdecimal() and len(answer) <= 8:
            index = int(answer)
            if 1 <= index <= len(options):
                return options[index - 1]
        print("Enter one of the listed numbers.")


def build_settings(data, allow_demo_writes):
    if not data.get("company") or data.get("currency") != "INR":
        raise ValueError("Select a dedicated INR company using --company.")
    probes = data.get("seed_field_readable", {})
    missing = [name for name in DOCTYPES if probes.get(name) != 200]
    if missing:
        raise ValueError("Fix custom_otc_seed_id or read access on: " + ", ".join(missing))

    masters = {key: list(dict.fromkeys(data.get(key, []))) for key in MASTER_FIELDS}
    choices = data.get("account_options", {})
    accounts = {
        key: list(dict.fromkeys(
            row["name"] for row in choices.get(key, {}).get("options", [])
            if row.get("account_currency") == "INR"
        ))
        for key in ACCOUNTS
    }
    missing = [key for key, options in {**masters, **accounts}.items() if not options]
    if missing:
        raise ValueError("No eligible records found for: " + ", ".join(missing))

    values = {"ERP_COMPANY": data["company"]}
    for key, options in masters.items():
        values[key] = select(key, options)
    used = set()
    for key, options in accounts.items():
        available = [name for name in options if name not in used]
        if not available:
            raise ValueError(f"No unused account for {key}; review your selections and rerun.")
        values[key] = select(key, available, choices[key].get("configured_company_default"))
        used.add(values[key])
    values.update(
        ERP_WAREHOUSE_1="P-WH1", ERP_WAREHOUSE_2="P-WH2",
        ERP_ORDER_REF_FIELD="custom_otc_seed_id",
        ERP_ALLOW_DEMO_WRITES="yes" if allow_demo_writes else "no",
    )
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("A discovered record name cannot be represented on one environment line.")
    return values


def update_env(path, text, values):
    remaining = dict(values)
    lines = []
    for line in text.splitlines():
        key, separator, _ = line.partition("=")
        if separator and key in values:
            if key in remaining:
                lines.append(f"{key}={remaining.pop(key)}")
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temp = Path(output.name)
            output.write("\n".join(lines) + "\n")
        os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", required=True, help="Exact ERPNext company name")
    parser.add_argument("--env", type=Path, default=here / "erp.env")
    parser.add_argument("--allow-demo-writes", action="store_true",
                        help="Set ERP_ALLOW_DEMO_WRITES=yes for your dedicated demo company")
    args = parser.parse_args()
    original = args.env.read_text(encoding="utf-8-sig")
    discover = here / "discover_erp.py"
    if not discover.is_file():
        raise ValueError(f"Place this script next to {discover.name}.")
    result = subprocess.run(
        [sys.executable, str(discover), "--env", str(args.env), "--company", args.company],
        stdout=subprocess.PIPE, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, check=True,
    )
    data = json.loads(result.stdout)
    if data.get("user") in (None, "", "Guest") or data.get("company") != args.company:
        raise ValueError("Discovery did not return the requested company for an authenticated user.")
    values = build_settings(data, args.allow_demo_writes)
    if args.env.read_text(encoding="utf-8-sig") != original:
        raise ValueError("erp.env changed during discovery. Rerun to use the current file.")
    update_env(args.env, original, values)
    print(f"Updated {args.env} for {args.company}. ERP_ALLOW_DEMO_WRITES={values['ERP_ALLOW_DEMO_WRITES']}.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError, EOFError, KeyboardInterrupt) as error:
        sys.exit(f"Could not populate erp.env: {error or 'selection cancelled'}")
