# OTC Gold notebooks

These notebooks build Gold facts, shared dimensions and reporting products from
one validated, published Silver release. Start with the
[Kimball design and Silver-to-Gold mappings](gold-kimball-design.md) for the
target grains, relationships and source keys.

## 1. Publish Silver and select the release

Run `silver/10_silver_reconcile_publish.ipynb` with your intended inputs and
cutoff. Verify its controls, set `PUBLISH_TARGET = "minio"`, and run the final
cell. It prints an exact URI such as
`s3://otc-silver/releases/<model_run_id>/manifest.json`. Use the 10 December
2026 Silver cutoff to check baseline AR. Use a separate 16 December Silver
release for the round-two observation.

Silver uses a dedicated `otc-silver` bucket on the existing development MinIO
server, with the S3 API at `http://127.0.0.1:19000`. The publisher creates the
bucket if it is missing and the credentials grant bucket creation. It writes
the manifest after the Delta tables. Open this release in MinIO under
`releases/<model_run_id>/tables/sales_order_line/`. You should see a
`_delta_log/` directory and Parquet data for a non-empty table. Gold reads
each table at the Delta version pinned in the named manifest and verifies
its row count and logical content checksum before building.

Both notebooks default to the existing development MinIO credentials from
`ingestion-airflow/platform.env`. Set `SILVER_S3_ENDPOINT`,
`SILVER_S3_ACCESS_KEY`, and `SILVER_S3_SECRET_KEY` in each process environment
to use another endpoint or scoped credentials. Silver needs write and read access
for publication and retries. Gold needs read access to release objects and permission to list the Delta table prefixes.

## 2. Prepare the Gold environment and settings

Extract the ZIP into your project root so `gold/` sits alongside `silver/`.
From the project root in Windows PowerShell, run:

```powershell
Set-Location gold
if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    python -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .\gold_settings.json)) {
    Copy-Item .\gold_settings.example.json .\gold_settings.json
}
code .
```

Open `gold_settings.json` and set `silver_manifest` to the exact **URI printed
by notebook 10**. The new Delta format gets a new release ID. Repoint Gold to
the new URI after publishing. Gold also accepts previous format-1 manifests
for MinIO JSONL releases and local exports. Relative filesystem paths resolve
from the Gold settings file.

| Setting | Default and behavior |
|---|---|
| `allow_blank_dealer_booking_status` | `true` applies the dealer fixture assumption that an exported order with blank status qualifies. Use `false` if the rule is unconfirmed. Baseline booking checks then expose the missing dealer booking. |
| `check_baseline` | `true` checks the named baseline orders. Set it to `false` only for a different dataset that excludes the fixture. |
| `history_complete` | `false` leaves lifetime billing and the 120-day inactivity flag unavailable. Enable only for an adequately covered reporting history. |
| `calendar_month_is_fiscal_period` | `false` requires confirmation of fiscal-period alignment before calculating dealer budget variance. |
| `channel_by_entity` | An empty object uses Unknown channel. Populate only with approved mappings for a fixture where each entity maps to one channel. |

Select `gold/.venv/Scripts/python.exe` as the kernel in VS Code. Run with the
notebook working directory set to `gold/`.

## 3. Run and review the notebooks

| Notebook | Review |
|---|---|
| `11_gold_bookings_fulfillment.ipynb` | Shared dimensions, bookings and fulfillment. Check two baseline booking lines, 11 pumps and ₹140,000. |
| `12_gold_finance_facts.ipynb` | Billing documents, payment allocations, AR and GL. Compare reported and calculated balances at the same cutoff. |
| `13_gold_products.ipynb` | Budget, order events and all seven reporting products. Review coverage alongside the measures. |
| `14_gold_validate_publish.ipynb` | Full build from the selected Silver release, controls, exceptions and optional publication. Expect ₹135,000 baseline net billed and posted GL revenue, and ₹100,000 baseline open AR at the 10 December cutoff. |

Use **Run All** for 11–14 in order for a walkthrough. Run **14** directly for a
complete build and validation. Each notebook independently reads the same pinned
Silver manifest. In notebook 14, set `PUBLISH_TARGET = "minio"` in the final
cell and rerun it. It publishes to `otc-gold` by default and prints the exact
`s3://otc-gold/releases/<gold_release_id>/manifest.json` URI. The manifest
records the source Silver run ID, Gold table paths, pinned Delta versions, row
counts, and logical checksums. Failed validation blocks publication. The
manifest is written after all tables have been committed and verified.

In the MinIO console, open the printed release under
`tables/fact_sales_order_booking/`. A populated table has Parquet files and a
`_delta_log/` directory. Coverage exceptions and validation results are
queryable as `gold_exceptions` and `gold_validation` Delta tables.

Gold defaults to the development MinIO credentials in
`ingestion-airflow/platform.env`. For scoped credentials or a different
endpoint, set `GOLD_S3_ACCESS_KEY`, `GOLD_S3_SECRET_KEY`, and
`GOLD_S3_ENDPOINT` in the process environment. The Gold publisher needs read,
list, and write access to its release prefixes. It creates the configured bucket
if absent and authorized. Set `gold_bucket` in `gold_settings.json` to override
`otc-gold`. Set `PUBLISH_TARGET = "local"` for a legacy filesystem export.

## 4. Run the same build without a notebook

From the `gold/` directory in PowerShell:

```powershell
.\.venv\Scripts\python.exe .\gold_pipeline.py --settings .\gold_settings.json
.\.venv\Scripts\python.exe .\gold_pipeline.py --settings .\gold_settings.json --publish
```

The first command validates. The second validates and publishes Delta to MinIO.
Use `--publish-local` for a local JSONL export. A pipeline
orchestrator can run the second command after Silver publishes, passing a
settings file that names that run's manifest. Add a separate promotion task if
a shared serving environment needs an active-release pointer.

## Scope of this local build

The notebooks read the pinned Silver Delta release directly from MinIO and
publish Gold facts, dimensions, marts, and controls as typed Delta tables to
the Gold bucket. Gold amounts and quantities are decimals, business dates are
dates, and event windows are UTC timestamps. Dimension keys are stable,
namespaced strings for this fixture. The release manifest identifies the
specific Silver input and Gold table versions for downstream BI.

- **Sales Daily** publishes day, legal entity and currency totals. Product,
  region, and gross-margin breakdowns need verified supporting attributes and
  billing/COGS attribution.
- **Customer 360** shows observed history by customer and currency. Its lifetime
  and inactivity measures require history coverage.
- **Live Orders** reconstructs windows from the landed event snapshot. It shows
  replay-safe counts across logical events and can be refreshed by rerunning the job.
- **AR Aging** preserves the selected balance basis and source coverage.
- **Collections** reports observed eligible due and cash by payment posting date.
  Collection rate and average days to pay are unavailable until application and
  settlement timing rules are approved.
- **P&L vs Budget** aligns the dealer revenue account when the calendar assumption
  is enabled. Parent category comparisons and a full P&L need scope coverage.
- **Revenue Reconciliation** compares fixture signed billing to mapped GL revenue
  and flags unmatched documents, differences and unlinked postings. The fixture
  assumes tax-free invoice totals. General tax treatment and timing-difference
  classifications require Finance policy.
