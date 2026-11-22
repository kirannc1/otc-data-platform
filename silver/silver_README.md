# OTC Silver notebooks

Use this folder to inspect the committed Bronze data, build the Silver model,
and review its reconciliation results. The notebooks read from MinIO. Notebook
01 can write a local inventory CSV, and notebook 10 can publish a local Silver
snapshot when explicitly enabled.

## 1. Place the files in the repository

On the Windows platform machine, extract `otc-silver-notebooks.zip` into the
**repository root**. The archive already contains a `silver/` directory. Save
this guide as `silver/README.md`.

| Repository location | What to check |
| --- | --- |
| `ingestion-airflow/compose.yaml` and `ingestion-airflow/platform.env` | Existing platform deployment and its MinIO credentials |
| `silver/01_bronze_inventory.ipynb` through `03_bronze_run_comparison.ipynb` | Existing Bronze discovery and comparison notebooks |
| `silver/05_silver_masters_orders.ipynb` through `10_silver_reconcile_publish.ipynb` | Silver walkthrough and execution notebooks from the new ZIP |
| `silver/bronze_review.py`, `silver/silver_*.py`, and `silver/requirements.txt` | Shared loaders, transformations, controls, and dependencies |
| `silver/compose.bronze-local.yaml` and `silver/silver_settings.example.json` | Local API port mapping and optional run settings |

Notebook `04_silver_order_model.ipynb` is an optional two-order walkthrough
distributed separately. If your folder has 01, 02, and 03, you can proceed
directly to 05 or 10 after extracting the new ZIP.

## 2. Verify Bronze is available

Run from the repository root in PowerShell. Start Docker Desktop beforehand.

```powershell
docker compose --env-file ingestion-airflow/platform.env -f ingestion-airflow/compose.yaml -f silver/compose.bronze-local.yaml up -d bronze
docker compose --env-file ingestion-airflow/platform.env -f ingestion-airflow/compose.yaml -f silver/compose.bronze-local.yaml ps bronze
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:19000/minio/health/ready).StatusCode
```

Expect HTTP `200` from port `19000`, the local S3 API. The MinIO console uses
port `19001`. Airflow uses `http://bronze:9000` inside Docker. Compose may
recreate the Bronze container to add the mapping and reuses the named data
volume. Reapply the override if a later base-only Compose deployment removes
the host port.

If the required inputs are already committed, reuse them. When loading a new
dataset, open Airflow at <http://127.0.0.1:18081>, run `otc_bronze_rest`,
`otc_bronze_sql`, `otc_bronze_files`, and `otc_bronze_kafka`, and verify each
run succeeded. For a new dealer export, publish it at the source before running
the Files DAG. The Bronze health check verifies connectivity. Notebook 01
verifies which manifests and payloads are available.

## 3. Select the Python kernel

Run from the repository root in PowerShell. The commands reuse an existing
`silver/.venv` when present.

```powershell
Set-Location silver
if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    python -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
code .
```

If `code` is unavailable in PowerShell, open the `silver/` folder from VS Code.
With the Microsoft Python and Jupyter extensions installed, open the desired
notebook and use **Select Kernel → Python Environments** to choose
`silver\.venv\Scripts\python.exe`. The notebook working directory should
be `silver/`, so Python can import the helpers and find the local settings file.

The Bronze helper reads `BRONZE_S3_ACCESS_KEY` and `BRONZE_S3_SECRET_KEY` from
`ingestion-airflow/platform.env`. Environment variables of the same names take
precedence. Keep the credentials in that environment file or environment.

## 4. Inspect or pin Bronze inputs

For a first run, open **01** and select **Run All**. Review the manifest and
payload status and look for missing collections or checksum mismatches. Use
**02** to inspect source masters and mapping candidates. **03** is for the
incremental Bronze test. Use baseline mode before seeding and compare mode
with an existing pre-seed checkpoint after the new ingestion runs complete.

Silver defaults to the latest completed REST, SQL, and POS manifests and the
latest dealer export by export cutoff. It loads landed Kafka deliveries and
expects one committed manifest for each Finance feed and the MDM fixture
unless you pin a choice. Review the selected manifest table at the start of
each Silver notebook.

For repeatable runs or to select among multiple manifests, copy the template
from the `silver/` folder. Skip the copy if you already have a settings file.

```powershell
if (-not (Test-Path .\silver_settings.json)) {
    Copy-Item .\silver_settings.example.json .\silver_settings.json
}
```

In `silver_settings.json`, set the relevant `manifests` values to exact Bronze
object keys ending in `/manifest.json`. Pin `kafka_keys` to exact delivery keys
when comparing a fixed set of events. `null` uses automatic selection.
The default AR cutoff is `2026-12-10` for the baseline. Use `2026-12-16` to
review the round-two dealer AR export at its own cutoff.

## 5. Run Silver in VS Code

For the walkthrough, open notebooks **05–10** in order and select **Run All**
in each. For a complete build and validation in one notebook, open **10**
directly. It rebuilds the earlier transformations in its own kernel.

| Notebook | What to review |
| --- | --- |
| `05_silver_masters_orders.ipynb` | Source masters, orders, and order lines. Check P-S1 and D-S1. |
| `06_silver_mdm.ipynb` | Approved and pending matches, account mappings, and conformed order lines |
| `07_silver_fulfillment_inventory.ipynb` | Split shipments, returns, and ERPNext Stock Entry legs |
| `08_silver_finance.ipynb` | Invoices, credits, payment allocations, GL postings, and observed AR |
| `09_silver_channels_bank_budget.ipynb` | Kafka deliveries and logical events, POS, bank match candidates, and budgets |
| `10_silver_reconcile_publish.ipynb` | Row counts, voucher and billing-to-GL checks, calculated AR, exceptions, and optional local publication |

## 6. Verify and publish the result

In notebook 10, review the controls and exceptions before changing the final
cell. `PASS` means the expected and actual values agree. `FAIL` shows a
difference to investigate. `NOT_COVERED` means there was insufficient
comparable evidence. For example, a dealer credit without an original invoice
link can reconcile to GL while its invoice-level AR calculation remains
uncovered.

For the named Metro baseline orders at the 10 December cutoff, expect
**₹135,000 net billing** and **₹100,000 open AR** across the parent and dealer.
These totals apply to those two fixture orders. Review the selected manifests
and cutoff before comparing them to a different run.

To save an output, set `PUBLISH_LOCAL = True` in the final cell of notebook 10
and run that cell. It prints the snapshot directory under
`silver/silver_output/<model_run_id>/`. Open its `manifest.json` to confirm
the selected Bronze keys, rule settings, output row counts, and checksums.
Review `reconciliation_control.jsonl` and `quality_exception.jsonl` alongside
the table files. Failed controls and error-level exceptions block publishing.
Rebuild with the recorded inputs and settings to reproduce the same snapshot.

## Troubleshooting

| Symptom | First check |
| --- | --- |
| Connection refused or wrong API port | Run the ready check in step 2. The notebook connects to port `19000` on the platform machine. |
| `ModuleNotFoundError` | Confirm the working folder is `silver/`, the Python helper files sit alongside the notebook, and the selected kernel has the dependencies from step 3. |
| Missing manifest or collection | Run 01 and check that the corresponding DAG published its payload. For dealer CSVs, check the upstream export before rerunning the Files DAG. |
| More than one eligible Finance or MDM manifest | Set the exact manifest key for that source in `silver_settings.json`. |
| AR result is missing or does not compare | Confirm that the exported snapshot and the calculation have the same cutoff, then review invoice and payment linkage. |

## Bronze source catalog

| Location | Configured contents | Meaning |
| --- | --- | --- |
| `batch/erpnext/` | Company, Customer, Item, UOM, Warehouse, Account | Legal entity, customers, items, units, locations, chart of accounts |
| `batch/erpnext/` | Sales Order, Stock Entry, Delivery Note, Sales Invoice, Payment Entry, GL Entry | Orders, stock movements, fulfillment, invoices and credits, payments, posted ledger lines |
| `batch/parent-pos/` | pos_receipts | Parent POS sales receipts |
| `batch/dealer-sql/` | customers, products, warehouses, accounts, sales_orders, order_lines, shipments, returns | Dealer masters and transactions |
| `files/otc-inbound/` | parent/finance, dealer/finance, dealer/exports, seed-v1 | Raw files copied using inbound manifests. The inventory identifies exact filenames and flags candidate journal, ledger, and mapping files. |
| `kafka/parent.orders.v1/` | One JSON envelope per generation, partition, and offset | Raw parent order event deliveries, including any repeated logical events at different offsets |

ERPNext `GL Entry` is the captured ledger collection. `Journal Entry` is absent
from the configured REST DocTypes. If journal files landed through the file DAG,
their keys appear in the live inventory when the notebook runs.

## Mapping scope

| Domain | Master evidence | Review |
| --- | --- | --- |
| Customer | ERPNext Customer, dealer customers | Source customer key and canonical customer ID |
| Product | ERPNext Item, dealer products | Source item code or SKU and canonical product ID |
| Warehouse | ERPNext Warehouse, dealer warehouses | Source location and entity scope, canonical warehouse ID |
| Company | ERPNext Company, inbound reference files if present | Source entity or code and canonical company ID |

Customer, Item, and UOM are site-wide masters on the dedicated ERPNext demo site.
Keep source context on their identifiers. Company discovery uses the ERPNext
Company record plus any inbound reference files. Dealer SQL has no configured
company table. Master records provide lookup evidence. A crosswalk explicitly
ties source identifiers to shared canonical identifiers.
