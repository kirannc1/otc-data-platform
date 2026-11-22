# Configurable OTC source seed

Set the count of bookings, shipments, invoices, payments, and POS receipts separately for the parent and dealer. Each run uses a batch ID so you can add additional transactions or retry the same batch without duplicating records.

## 1. Enter the counts

Extract the ZIP into your repository root on the source machine. From that root in PowerShell, copy the example file once.

```powershell
Copy-Item .\source-setup\seed\seed-volume.example.json .\source-setup\seed\seed-volume.json
code .\source-setup\seed\seed-volume.json
```

| Setting | Behavior |
|---|---|
| `batch_id` | Use `VOL001`, then `VOL002`, and so on. Prefixes such as `P-VOL001-` and `D-VOL001-` identify records for verification. |
| `posting_date` | All new bookings, warehouse transactions, billing, and payments post on this date. Choose a date within an open ERP fiscal period on or before today. |
| `ar_cutoff` | Dealer open-AR snapshot date and the intended Silver reporting cutoff. Due dates are spread across 14, 30, and 45 days after posting. |
| `parent.bookings`, `dealer.bookings` | Exact sales-order counts, with one line per order. The parent alternates existing pump and valve customers and products. The dealer uses its existing pump master. |
| `shipments` | Exact delivery count for the entity. A delivery needs a booking. When deliveries exceed bookings, some orders get split shipments. When deliveries are fewer, some orders remain unshipped. |
| `invoices` | Exact invoice count, up to the shipment count. The fixture creates at most one invoice per shipment. |
| `payments` | Exact payment count, up to the invoice count. Each payment allocates 50% of one invoice. |
| `pos_receipts` | Independent counter sales. Parent receipts land via Commerce REST. Dealer receipts, lines, and tenders land via the dealer export. |
| `parent_booked_acceptances`, `dealer_booked_acceptances` | Optional acceptance for a subset of the respective bookings. The parent uses Kafka. Dealer acceptances land via export. |
| `parent_cancelled_orders` | Number of storefront-only orders that get an acceptance and a cancellation event. Each adds two Kafka events. |

Zero is valid for any count, subject to the dependencies above. Posted documents generate their own ledger entries. For every dealer invoice the seed creates one AR snapshot at the selected cutoff. The fixture adds no new master records, credits, bank statements, or budget inputs.

## 2. Preview, preflight, and apply

The preview works offline with the existing root `.venv`. It prints row counts and expected booking, billing, applied-cash, and open-AR totals for your batch.

```powershell
$Config = '.\source-setup\seed\seed-volume.json'
.\source-setup\seed\Invoke-VolumeSeed.ps1 -Config $Config -Preview
```

For preflight and apply, keep the relevant existing forwards online. Defaults are ERPNext on `18080` from `erp.env`, Commerce on `8000`, and PostgreSQL on `15432`. Existing prerequisites are the demo ERP company and seed API permissions, the product/customer fixtures from `P-S1` and `P-S2`, and the seeded dealer tables. The launcher reads PostgreSQL credentials from the existing Kubernetes Secret. Its default context is `kind-otc`.

```powershell
.\source-setup\seed\Invoke-VolumeSeed.ps1 -Config $Config
# After preflight succeeds
.\source-setup\seed\Invoke-VolumeSeed.ps1 -Config $Config -Apply
```

Run one seed launcher at a time. If interrupted, rerun the same config and batch ID on the same source machine. The local `.volume-seed-state` directory records the original configuration and target for applied batch IDs. Preserve it across retries. For new counts or a new date, choose a new batch ID. Keep the local state and your `erp.env` out of Git.

The launcher verifies the batch's Kafka outbox if it includes parent events. If it writes dealer rows, it publishes the full dealer export to MinIO as `dealer/exports/dealer-export-vol001/manifest.json` for batch `VOL001`. A completed export Job is reused on retry. For a terminally failed Job, fix the cause and rerun with the same config and a new export Job name using `-ExportJob dealer-export-vol001-retry1`.

## 3. Verify in Bronze and DuckDB

Trigger the relevant Bronze DAGs after apply succeeds. Use the new ERP, parent POS, dealer SQL, and dealer export manifests in Silver wherever those sources changed. Include the new Kafka keys alongside prior keys if you pin them. For AR, set Silver to the same `ar_cutoff` as the batch, then publish Silver and Gold and rebuild DuckDB from the new Gold manifest.

For the included `VOL001` example, expect **16 bookings**, **17 shipments**, **12 invoices**, **6 payments**, **5 POS receipts**, and **9 parent Kafka events**. The batch control totals are **₹341,000 booked**, **₹224,500 billed**, **₹58,750 applied cash**, and **₹165,750 open AR**.

In DuckDB, check bookings by batch reference. An order line with split deliveries contributes one booking amount.

```sql
SELECT legal_entity_id, currency,
       count(DISTINCT sales_order_id) AS booked_orders,
       sum(booked_amount) AS booked_amount,
       sum(linked_shipments) AS linked_shipments
FROM sem_order_lines
WHERE starts_with(order_reference, 'P-VOL001-')
   OR starts_with(order_reference, 'D-VOL001-')
GROUP BY legal_entity_id, currency;
```

To isolate the billing and AR delta, compare Gold releases at the same cutoff or use a posting date reserved for that batch.
