# OTC Silver discovery

Use this folder to extract and inspect the existing Bronze data, document its
business meaning, and review four reference mappings. These notebooks write a
local inventory and display source records as pandas DataFrames. They make no writes to MinIO or the source systems.

## Run in VS Code on Windows

Place `silver/` alongside `ingestion-airflow/` on the platform machine. Run the
following from their common parent directory in PowerShell.

```powershell
docker compose --env-file ingestion-airflow/platform.env -f ingestion-airflow/compose.yaml -f silver/compose.bronze-local.yaml up -d bronze
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:19000/minio/health/ready
```

The override publishes the Bronze S3 API at `http://127.0.0.1:19000` on this
machine. Compose may briefly recreate the Bronze container to add the port.
Its named data volume is reused. Airflow still connects over Docker’s internal
network at `http://bronze:9000`. Apply the override after any base-only Compose
deployment that removes the host mapping. The S3 API and the MinIO console
have separate endpoints.

```powershell
Set-Location silver
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Open `silver/` in VS Code with the Microsoft Python and Jupyter extensions.
Select `.venv\Scripts\python.exe` as the kernel and run the notebooks in order.
The helper reads `BRONZE_S3_ACCESS_KEY` and `BRONZE_S3_SECRET_KEY` from
`ingestion-airflow/platform.env`. Environment variables of the same names take
precedence. The notebooks use S3 list and get operations. Run them after the
Bronze DAGs finish to review a stable set of objects.

1. **01_bronze_inventory.ipynb** reads the Bronze bucket directly, lists every
   object, checks published payloads against their manifests, and shows collection
   and field coverage. It writes `bronze_inventory.csv` locally. Historical
   snapshot counts are listed per run. Use the latest complete snapshot per
   source when inspecting current master data. The **Open the actual records**
   cell displays dealer customer rows by default. Change the source and
   collection, or supply a Bronze object key, to explain another dataset.
2. **02_reference_mappings.ipynb** inspects customer, product, warehouse, and
   company masters alongside candidate mapping files as separate DataFrame
   previews. Confirm the authoritative
   crosswalk and its key columns before any joins. Domain tags are based on
   filename or header evidence. A generic crosswalk can appear in several views.

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
