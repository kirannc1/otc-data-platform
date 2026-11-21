# Implementation README

## Source systems and local technology stack

A private equity-backed equipment distributor has acquired a regional dealer. Both companies sell through e-commerce, direct sales, and branch point of sale. The local implementation deploys ERPNext for the parent distributor and a separate PostgreSQL database to simulate the acquired dealer’s legacy ERP.

| Data category | What we bring in | Parent distributor · stack and source type | Acquired regional dealer · stack and source type |
|---|---|---|---|
| **E-commerce orders** | Order acceptance, changes, and cancellations, including order lines, quantities, values, and timestamps | **FastAPI storefront simulator → PostgreSQL outbox → Kafka.** The API stores orders in PostgreSQL and an outbox publisher sends lifecycle events to Kafka. **Streaming source.** | **PostgreSQL storefront tables → Python export → MinIO.** Deliver nightly CSV files with orders, lines, and status changes. **Batch file source.** |
| **Sales & fulfillment** | Sales orders, delivery notes, shipment lines, and physical returns | **ERPNext → REST API.** Pull changed documents on a schedule. **Batch API source.** | **PostgreSQL legacy ERP simulator → read-only SQL extracts.** Pull order, shipment, and return tables on a schedule. **Batch database source.** |
| **Billing & accounting** | Invoices, credit notes, due dates, payment receipts, allocations, open balances, and general ledger entries | **ERPNext → REST API.** Extract posted accounting records and their references. **Batch API source.** | **PostgreSQL legacy ERP simulator → Python exports → MinIO.** Deliver separate billing, receivables, and GL files with their own reporting cutoffs. **Batch file source.** |
| **Master data** | Customer accounts, product codes, units of measure, branches, and chart of accounts | **ERPNext → REST API.** Extract master records using parent identifiers. **Batch API source.** | **PostgreSQL legacy ERP simulator → read-only SQL extracts.** Use different customer, product, and account codes to exercise cross-company mappings. **Batch database source.** |
| **Point of sale** | Branch receipts, items sold, returns, discounts, taxes, and tenders | **FastAPI POS endpoint backed by PostgreSQL.** Expose POS records as a separate feed from the commerce simulator. **Batch API source.** | **PostgreSQL POS tables → Python export → MinIO.** Deliver end-of-day receipt, line, and tender files. **Batch file source.** |
| **Finance files** | Bank statement transactions and approved budgets | **Python fixture generator → parent Finance inbound location in MinIO.** Deliver bank statements and budget files. **Batch file source.** | **Python fixture generator → dealer Finance inbound location in MinIO.** Use a different statement layout and the dealer’s account codes. **Batch file source.** |

## Local deployment choices

- Reuse one PostgreSQL instance with separate databases and credentials for parent commerce, the dealer simulator, and platform metadata.
- Use one Kafka broker and one MinIO instance. Give each company a distinct inbound location and keep inbound files separate from platform-managed data.
- Keep Redis optional for storefront sessions or caching. PostgreSQL stores durable orders and Kafka carries their events.
- Preserve the legal entity and original record IDs in every applicable feed. Record delivery cadence and reporting cutoffs so delayed or missing coverage is visible.

## First implementation slice

Start with **the parent’s ERPNext API**, **the parent storefront’s Kafka stream**, and **the dealer’s billing, receivables, and GL files in MinIO**.

Seed one parent online order with the same external order ID in Kafka and ERPNext. Seed one dealer invoice for a customer who also has an outstanding parent invoice. Verify that **Commercial Performance** counts the online booking once and **Working Capital & Revenue Assurance** shows combined customer exposure while preserving the legal entity that issued each invoice.

## Deploy the sources on kind

The bundle deploys the source services in `otc-sources` and ERPNext with MariaDB and Redis in `otc-erp`. Each stack has its own persistent storage and credentials. Use PowerShell from the extracted project root.

### 1. Prepare the clean deployment

If you are resetting a disposable demo, wait until the namespaces you deleted are gone. Namespace deletion can remove their PVC-backed data, including ERPNext documents, Kafka events, and MinIO objects. Keep the existing kind cluster and confirm your target is `kind-otc`.

```powershell
kubectl --context kind-otc get nodes
kubectl --context kind-otc get storageclass
kubectl --context kind-otc get namespace
if (-not (Test-Path .\source-setup\k8s\credentials.env)) {
    Copy-Item .\source-setup\k8s\credentials.env.example .\source-setup\k8s\credentials.env
}
```

Fill the source credential file with local values and make both DSNs match their database role passwords. Use URL-safe letters and digits in database passwords for this deployment.

| Path | Contents |
|---|---|
| `source-setup/k8s/` | Source manifests, PostgreSQL initialization, Finance Job and dealer export CronJob |
| `source-setup/services/` | Local images for MinIO, Commerce, dealer export and Finance files |
| `source-setup/erpnext/` | ERPNext bootstrap Dockerfile and Kubernetes manifest |
| `source-setup/seed/` | ERP discovery and environment configuration, seed loaders, and read-back verification |

### 2. Build and load images

```powershell
$Images = [ordered]@{
    'otc-minio:2025-10-15' = '.\source-setup\services\minio'
    'otc-commerce:0.1.0' = '.\source-setup\services\commerce'
    'otc-dealer-export:0.1.0' = '.\source-setup\services\dealer-export'
    'otc-finance-files:0.1.0' = '.\source-setup\services\finance-files'
    'otc-erpnext-bootstrap:v16.60.0' = '.\source-setup\erpnext'
}
foreach ($Image in $Images.Keys) {
    docker build -t $Image $Images[$Image]
    if ($LASTEXITCODE -ne 0) { throw "Build failed: $Image" }
    kind load docker-image --name otc $Image
    if ($LASTEXITCODE -ne 0) { throw "kind image load failed: $Image" }
}
```

If you already built the exact first four tags, you can reuse those images. The ERPNext image includes a copy of the sites template outside the PVC mount.

### 3. Deploy sources and initialize Kafka and MinIO

```powershell
& {
    kubectl --context kind-otc apply -k .\source-setup\k8s
    if ($LASTEXITCODE -ne 0) { throw 'Source manifests failed.' }

    foreach ($Workload in @('statefulset/postgres','statefulset/kafka','statefulset/minio','deployment/commerce','deployment/outbox')) {
        kubectl --context kind-otc -n otc-sources rollout status $Workload --timeout=180s
        if ($LASTEXITCODE -ne 0) { throw "Not ready: $Workload" }
    }

    kubectl --context kind-otc -n otc-sources exec kafka-0 -- /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic parent.orders.v1 --partitions 1 --replication-factor 1
    if ($LASTEXITCODE -ne 0) { throw 'Kafka topic creation failed.' }

    kubectl --context kind-otc -n otc-sources apply -f .\source-setup\k8s\finance-files.yaml
    if ($LASTEXITCODE -ne 0) { throw 'Finance Job apply failed.' }
    kubectl --context kind-otc -n otc-sources wait --for=condition=complete job/finance-files --timeout=120s
    if ($LASTEXITCODE -ne 0) { throw 'Finance Job did not complete.' }
}
```

The Finance Job creates the inbound bucket and writes the bank and budget files. Run the dealer export after seeding. Commerce smoke checks are read-only. Reserve P-S1 and P-E1 for the ten-pump seed.

### 4. Deploy ERPNext

Create the namespace and the bootstrap Secret before applying the manifest. Use the MariaDB root password already stored in the database if reusing an initialized PVC. On a fresh deployment, choose new passwords and retain the Administrator password for the setup wizard.

```powershell
& {
    kubectl --context kind-otc create namespace otc-erp --dry-run=client -o yaml |
        kubectl --context kind-otc apply -f -
    if ($LASTEXITCODE -ne 0) { throw 'ERP namespace apply failed.' }

    $Existing = kubectl --context kind-otc -n otc-erp get secret erpnext-bootstrap --ignore-not-found -o name
    if ($LASTEXITCODE -ne 0) { throw 'ERP secret check failed.' }
    if (-not $Existing) {
        $DbPassword = [System.Net.NetworkCredential]::new('', (Read-Host 'MariaDB root password' -AsSecureString)).Password
        $AdminPassword = [System.Net.NetworkCredential]::new('', (Read-Host 'ERPNext Administrator password' -AsSecureString)).Password
        if (-not $DbPassword -or -not $AdminPassword) { throw 'Both passwords are required.' }
        kubectl --context kind-otc -n otc-erp create secret generic erpnext-bootstrap "--from-literal=db-root-password=$DbPassword" "--from-literal=admin-password=$AdminPassword"
        if ($LASTEXITCODE -ne 0) { throw 'ERP bootstrap Secret creation failed.' }
        Remove-Variable DbPassword, AdminPassword
    }

    kubectl --context kind-otc apply -f .\source-setup\erpnext\erpnext.yaml
    if ($LASTEXITCODE -ne 0) { throw 'ERP manifest apply failed.' }
    kubectl --context kind-otc -n otc-erp rollout status deployment/erpnext --timeout=600s
    if ($LASTEXITCODE -ne 0) {
        kubectl --context kind-otc -n otc-erp logs deployment/erpnext -c initialize-site --tail=120
        throw 'ERPNext did not become ready.'
    }
}
```

Start the ERP port-forward in its own window, then check the HTTP endpoint from another.

```powershell
kubectl --context kind-otc -n otc-erp port-forward --address 127.0.0.1 svc/erpnext 18080:8080
```

```powershell
curl.exe -fsS http://127.0.0.1:18080/api/method/ping
```

Expect `{"message":"pong"}`. Sign in at [ERPNext](http://127.0.0.1:18080), complete the setup wizard, and follow **Load the first seed** for the demo company, custom fields, API credentials, and first seed.

### Local operating boundaries

The simulator endpoints are for synthetic local data and have no application authentication. Keep them on internal services or loopback port forwards. The source jobs use MinIO bootstrap credentials in this local package. Use scoped bucket identities, broker authentication, and network policy before using a shared cluster. ERPNext runs Redis for cache and queue in otc-erp. The other source workloads use PostgreSQL, Kafka, and MinIO.

## Source data at a glance

Both companies provide the same core order-to-cash data. The parent uses ERPNext and the acquired dealer uses its PostgreSQL ERP simulator. Each company retains its own source IDs.

- **Sales and fulfillment** covers orders, product lines, quantities, and shipment details. The parent sells 10 pumps on one order and ships them in two batches of six and four.
- **Billing and accounting** covers invoices, credits, payments, the invoices each payment is applied to, outstanding balances, and general ledger entries.
- **Master data and mappings** cover customers, products, warehouses, units of measure, and account codes. Approved master data mappings link shared customers and products across companies for group reporting.
- **Storefront and point of sale** use FastAPI for the parent. The storefront records online orders and publishes order events to Kafka through its outbox. The POS API exposes counter receipts, items sold, returns, and payment details. The dealer delivers equivalent storefront and POS records through scheduled files.
- **Object storage** uses MinIO for the dealer’s storefront, POS, billing, receivables, and GL exports. It also holds both companies’ bank statements and budgets, plus documents for mapping reviews and Finance policy lookup.

An online order can appear in both Kafka and ERPNext. The shared order reference links those records so the booking counts once. The bank statement shows that cash moved, and the ERP payment allocation shows which invoice it paid.

## Revised seed dataset

### 1. Master records and approved mappings

Each company gets five customers and five products. Two pairs match automatically, two pairs require steward approval, and one record from each company is distinct. Each master domain has six group records after review. While review is pending, entity reporting continues to show the local IDs.

| Customer case | Parent ERPNext | Acquired dealer PostgreSQL | Expected group result |
|---|---|---|---|
| Automatic 1 | P-C1 · Metro Industrial Services | D-C1 · Metro Ind. Services | G-CUST-001, verified registration key |
| Automatic 2 | P-C2 · Pioneer Engineering | D-C2 · Pioneer Engineering | G-CUST-002, verified registration key |
| Manual 1 | P-C3 · Apex Industrial Supply | D-C3 · Apex Industrial | G-CUST-003 after registration review |
| Manual 2 | P-C4 · Summit Process Systems | D-C4 · Summit Process | G-CUST-004 after registration and address review |
| Company-specific | P-C5 · Harbor Utilities | D-C5 · Valley Fabrication | G-CUST-005 and G-CUST-006 |

| Product case | Parent ERPNext | Acquired dealer PostgreSQL | Expected group result |
|---|---|---|---|
| Automatic 1 | P-PUMP · ACME RP100, each | D-PUMP · ACME RP100, each | G-PROD-001, matching manufacturer, part, and unit |
| Automatic 2 | P-VALVE · ACME V50, each | D-VALVE · ACME V50, each | G-PROD-002, matching manufacturer, part, and unit |
| Manual 1 | P-FILT · ACME F200, each | D-FLT · F200 filter, manufacturer missing | G-PROD-003 after supplier catalog review |
| Manual 2 | P-SEAL · ACME S20, each | D-SEAL · ACME S20, pack of 10 | G-PROD-004 after approval of one pack = 10 each |
| Company-specific | P-GAUGE · Pressure gauge | D-HOSE · Hydraulic hose | G-PROD-005 and G-PROD-006 |

Seed three warehouses. The parent has its main distribution warehouse P-WH1 and branch warehouse P-WH2. The acquired dealer has its regional warehouse D-WH1. Keep all three distinct and roll each up to its owning company. Seed sufficient opening stock for each planned shipment. Finance approves mappings from local Sales, Receivables, Bank, Inventory, and COGS accounts to group reporting categories. The dealer uses 400-D, 110-D, and 100-D for Sales, Receivables, and Bank.

### 2. Orders, shipments, billing, and ledger

| Record | Parent ERPNext | Acquired dealer PostgreSQL |
|---|---|---|
| Order and line | P-S1 · P-C1 buys 10 × P-PUMP at ₹10,000 each. ₹100,000 booked. | D-S1 · D-C1 buys 1 × D-PUMP. ₹40,000 booked. |
| Shipment | Six pumps from P-WH1 on 3 Nov and four from P-WH2 on 5 Nov. Both deliveries reference the original line. | D-DEL1 · One pump from D-WH1 on 5 Nov against D-S1. |
| Invoice | Invoice A ₹60,000, due 3 Dec. Invoice B ₹40,000, due 5 Dec. Each references its delivery. | D-I1 · ₹40,000, due 5 Dec, linked to D-S1 and its shipment. |
| Payment and allocation | P-P1 · ₹25,000 on 10 Nov applied to invoice A. | D-R1 · ₹10,000 on 12 Nov applied to D-I1. Bank reference D-P1. |
| Credit | ₹5,000 price allowance on 15 Nov against invoice B. Shipped quantity remains 10. | Baseline has no credit. |
| Ledger | ERPNext generates invoice, payment, and allowance postings using parent accounts. | Two journal lines for the invoice and two for the receipt using dealer accounts. |
| Open AR at 10 Dec | ₹35,000 on invoice A and ₹35,000 on invoice B. | ₹30,000 on D-I1. |

- **Second automatic match.** Seed one Pioneer valve order in each ERP. Both roll up to the same group customer and product on the first run.
- **Manual matches.** Seed an Apex filter order and a Summit seal order in each ERP. Test before and after steward approval. One dealer seal pack reports as 10 each after approval. The source unit and amount stay intact.
- **Company-specific masters.** Seed a Harbor gauge order in the parent ERP and a Valley hose order in the dealer ERP. They stay distinct in group reporting.

### 3. Storefront, Kafka, POS, and files

| Destination | Sample data to seed |
|---|---|
| Parent storefront · FastAPI and PostgreSQL | P-S1 links to ERPNext and carries P-C1, P-PUMP, 10 units, and ₹100,000. |
| Parent Kafka · `parent.orders.v1` | Publish acceptance P-E1 keyed by P-S1. Deliver twice at different offsets for the replay test. Expect one booking. |
| Dealer storefront · PostgreSQL and MinIO | Store D-S1, its line, and acceptance D-E1. Export them nightly. The external order reference links the files to the dealer ERP order. |
| Parent POS · FastAPI and PostgreSQL | P-POS1 · ₹10,000 counter sale with a receipt, P-PUMP item line, and card tender. |
| Dealer POS · PostgreSQL and MinIO | D-POS1 · ₹10,000 counter sale with a receipt, D-PUMP item line, and card tender. Export receipt, item, and tender records separately. |
| Dealer ERP exports · MinIO | Invoice, receipt, allocation, AR snapshot, and GL files linked to D-I1 directly or through its receipt and allocation. Include cutoff, row counts, and checksums. |
| Finance files · MinIO | P-B1 matches the parent’s ₹25,000 payment. D-B1 matches the dealer’s ₹10,000 payment. Include the ₹120,000 parent product-category budget and ₹50,000 dealer sales-account budget for November at their respective grains. |
| Stewardship and policy documents · MinIO | Catalog evidence for the filter and seal pack size, synthetic customer registration evidence, and versioned AR aging and revenue reconciliation policies. Publish approved mapping decisions as versioned crosswalks. |

**Baseline check.** At 10 December, the Metro invoices produce ₹135,000 in net invoiced sales, ₹135,000 in posted revenue, and ₹100,000 in overdue AR. POS receipts and the extra mapping scenarios have separate assertions.

## Seed data by source system

The parent seeds ERP records in ERPNext. The acquired dealer seeds equivalent records in its PostgreSQL legacy ERP simulator. Storefront events and dealer CSVs are produced from those source workflows.

### Master records and approval mappings

| Data type | Parent seed destination | Acquired dealer seed destination | Platform intake |
|---|---|---|---|
| Customers | ERPNext Customer records P-C1 through P-C5 | Dealer PostgreSQL customers D-C1 through D-C5 | Parent REST API and dealer read-only SQL |
| Products and units | ERPNext Items and UOMs for pump, valve, filter, seal, and gauge | Dealer PostgreSQL products and UOMs for pump, valve, filter, seal pack, and hose | Parent REST API and dealer read-only SQL |
| Warehouses | ERPNext Warehouses P-WH1 and P-WH2 | Dealer PostgreSQL warehouse D-WH1 | Parent REST API and dealer read-only SQL. Keep all three warehouse identities. |
| Chart of accounts | ERPNext Sales, Receivables, Bank, Inventory, and COGS accounts | Dealer PostgreSQL equivalents, including 400-D, 110-D, and 100-D | Parent REST API and dealer read-only SQL |
| Group masters and crosswalks | Parent local IDs supply one side of each mapping | Dealer local IDs supply the other side | Platform MDM/reference store. Per domain, resolve two automatic matches and two steward-reviewed matches, then approve one unique record per company. Publish six group customers, six group products, and Finance-approved account mappings. |
| Mapping evidence | Synthetic customer registration and supplier catalog references | Synthetic evidence for the customer reviews, filter manufacturer, and seal pack size | MinIO documents linked to their mapping reviews |

### Orders, shipments, billing, and ledger

| Data type | Parent seed destination | Acquired dealer seed destination | Platform intake |
|---|---|---|---|
| Sales orders and lines | ERPNext Sales Order and item rows for the 10-pump order with external reference P-S1 | Dealer PostgreSQL order D-S1 and its one-pump line | Parent REST API and dealer read-only SQL |
| Shipments and physical returns | ERPNext Delivery Notes for six pumps from P-WH1 and four from P-WH2. Use a separate physical-return fixture. | Dealer PostgreSQL shipment D-DEL1 from D-WH1. Use a separate return row for that scenario. | Parent REST API and dealer read-only SQL |
| Invoices and credits | ERPNext Sales Invoices A and B and the ₹5,000 price allowance against B | Dealer PostgreSQL invoice D-I1. Seed the late-credit case separately. | Parent REST API and dealer billing CSV in MinIO |
| Payments and allocations | ERPNext payment P-P1 for ₹25,000 applied to invoice A | Dealer PostgreSQL receipt D-R1 for ₹10,000 allocated to D-I1, with bank reference D-P1 | Parent REST API and dealer receipt and allocation CSVs in MinIO |
| General ledger | ERPNext generates journal lines when source documents are posted | Seed balanced journal lines in dealer PostgreSQL | Parent REST API and dealer GL CSV in MinIO |
| Open receivables | Derive ₹35,000 open per invoice at the 10 December cutoff from posted documents and allocations | Seed D-AR1 with ₹30,000 open against D-I1 at the same cutoff | Parent ERP records via REST API and dealer AR snapshot CSV in MinIO |

### Storefront, Kafka, POS, and files

| Data type | Parent seed destination | Acquired dealer seed destination | Platform intake |
|---|---|---|---|
| Storefront orders and status | Submit P-S1 through storefront FastAPI. It persists the order, line, and outbox event P-E1 in commerce PostgreSQL. | Seed D-S1, its line, and acceptance D-E1 in dealer PostgreSQL storefront tables. | Parent Kafka stream and dealer nightly CSV in MinIO |
| Kafka replay | Publish P-E1 keyed by P-S1 and deliver the same event at a second offset. | Use the dealer storefront status export for this fixture. | Parent Kafka and dealer batch files |
| POS receipts, lines, and tenders | Submit P-POS1, a ₹10,000 card sale, through POS FastAPI backed by commerce PostgreSQL. | Seed D-POS1, a separate ₹10,000 card sale, in dealer receipt, line, and tender tables. | Parent POS REST API and dealer end-of-day CSVs in MinIO |
| Bank statements | Put bank row P-B1 in the parent MinIO inbound location, with ₹25,000 and reference P-P1. | Put D-B1 in the dealer MinIO inbound location, with ₹10,000 and reference D-P1. | Separate bank CSVs per company |
| Budgets | Put the ₹120,000 November PUMP category budget in the parent MinIO inbound location. | Put the ₹50,000 November 400-D sales-account budget in the dealer MinIO inbound location. | Versioned budget CSVs at their respective reporting grains |
| Finance policies | Provide group AR aging and revenue reconciliation policies. | Provide dealer-specific accounting or mapping guidance. | Versioned MinIO documents with effective dates and access scope |

**Seed order.** Load local masters, then orders, shipments, invoices, and payments. Add storefront and POS fixtures, publish dealer exports and Finance files, then review and publish the MDM crosswalks. Link storefront and ERP orders using P-S1 and D-S1. Keep the mapping scenarios and POS receipts identifiable separately from the Metro baseline of ₹100,000 overdue AR and ₹0 invoice-to-GL difference.

## Proposed seed volumes

Start with five ERP orders per company. After verifying both data products, expand to 60 orders per company over three months. Keep the same five customers and five products per company so the larger run tests repeat purchases, payment behavior, and overdue balances using the same mapping cases.

| Source | First seed | Three-month demo |
|---|---|---|
| Parent ERPNext | Five customers, five products, two warehouses, five mapped account categories, and five orders. Complete the Metro order with two shipments, two invoices, one payment, and one credit. | 60 orders. Aim for 55 fulfilled orders, including 10 with two shipments, for 65 shipment records. Target about 60 invoices, 45 receipts, and five credits. ERPNext generates ledger entries from posted documents. |
| Dealer PostgreSQL ERP | Five customers, five products, one warehouse, and five mapped account categories under dealer IDs. Seed five orders. Complete the Metro order with one shipment, one invoice, one receipt, one allocation, and one AR snapshot row. | 60 orders with comparable fulfillment and billing volumes and different shipment and payment patterns. Export invoice-level AR at three month-end cutoffs. |
| Parent storefront and Kafka | One online order linked to ERPNext. Deliver its acceptance twice to test replay. | 20 of the 60 parent ERP orders originate online. Publish 20 acceptances and five subsequent changes or cancellations. Add five replay deliveries for 25 distinct events and 30 total deliveries. |
| Dealer storefront and MinIO exports | One online order and one acceptance record. Export billing, receipts, allocations, AR, and GL for the baseline cutoff. | 15 of the 60 dealer ERP orders originate online. Export 15 acceptances and five subsequent status changes. Provide three month-end Finance export sets and a separate late-GL delivery for testing. |
| Parent POS API and dealer POS files | One receipt per company with one item line and one card tender. | 10 receipts per company, including a split-tender case and a return. Add corresponding ERP postings before including POS receipts in ERP reconciliation totals. |
| Bank statements and budgets in MinIO | Per company, one statement with one matching payment row and one approved budget row. | Per company, three monthly statements with about 50 rows total, covering matched receipts, unmatched movements, and delayed confirmations. Add three approved monthly budget rows at each company's reporting grain and one superseded version to test selection. |
| MDM crosswalks and evidence | For each of customers and products, 10 local records yield six group records after review. Include two automatic pairs, two manual pairs, and two company-specific records. Retain three distinct warehouses and map five account categories across both companies. | Reuse the masters across repeat transactions. Keep the four manual pair decisions and their evidence traceable. Compare group reporting before and after approval. |
| Policy documents and Redis | One AR aging policy, one revenue reconciliation policy, and MDM evidence documents. Redis needs no business seed records. | Add a second version of one policy to test which version applies at the reporting cutoff. |

The first seed has **10 ERP orders across both companies**. Complete the two Metro orders through billing and payment. Use the other eight orders to test automatic matches, manual review, and company-specific records. Keep the Metro fixture identifiable so its **₹100,000 overdue AR and ₹0 invoice-to-GL difference** can still be verified in the larger dataset.

## Load the first seed

This run loads the ten-order fixture. The four non-Metro parent orders are drafts for master-data checks. The dealer baseline is initialized by PostgreSQL bootstrap and the seed command adds its remaining records. Use the three-month volume plan for a later load.

### 1. Complete the prerequisites on each source

Start with the source deployments already running. Finish the source-side work below before filling the seed environment or running any loaders.

| Source | Prerequisite on the source system | What the seed will write later |
|---|---|---|
| ERPNext (parent) | Complete the setup wizard. Create or confirm a dedicated demo Company with INR and a chart of accounts. Verify an open fiscal period covering 1–15 November 2026. Provision the masters, custom fields and API user listed below. | Parent customers, items, two leaf warehouses, sales orders, stock and delivery transactions, invoices, receipt, and credit note. ERPNext generates the parent ledger entries. |
| Dealer PostgreSQL | The deployment's initial database setup must have created `dealer_legacy`, its tables, the database owner, and `dealer_reader`. Confirm the owner can add fixture columns and write rows, and the reader can query the export tables. | The dealer loader extends the starter fixture with the remaining orders, master records, and warehouse data. |
| Commerce storefront, POS, and outbox | Confirm Commerce answers `/health`, PostgreSQL has the `commerce` database, and the outbox Deployment is running. The supplied local API needs no separate API credential. Keep fixture ID P-E1 available for the ten-pump order. | The loader posts the storefront order and POS receipt. The outbox publishes the Kafka event. |
| Kafka | Create or describe `parent.orders.v1` using the deployment commands above. The supplied local listener needs no separate seed credential. Confirm the outbox can use the broker. | The event comes from the outbox. A second delivery is used only for the optional replay test. |
| MinIO | Confirm the local access credentials work and `otc-inbound` exists. Run the Finance Job in the deployment section if needed. That Job creates the bucket and writes the starter bank and budget CSVs. | The document loader writes MDM candidates, policy documents and mappings. The dealer export Job writes a snapshot after the dealer seed. |
| Dealer export | Confirm the `dealer-export` CronJob exists and has MinIO credentials and the dealer read-only DSN. | Start a new Job after dealer seeding, so its snapshot includes the completed dataset. |

**ERPNext source setup, in order**

1. Create or confirm the company and fiscal year first. Choose or create existing leaf Customer Group, Territory, and Item Group records and an enabled UOM. Confirm a group Warehouse for that company and an active posting Cost Center.
2. In Chart of Accounts, verify five distinct enabled posting accounts for Bank, Receivables, Sales, COGS and Inventory. Align the company's default inventory account with the Inventory value you will put in `erp.env`. Configure the demo scenario without automatic taxes.
3. In Customize Form, add the unique Data field `custom_otc_seed_id` to Sales Order, Stock Entry, Delivery Note, and Sales Invoice. Use the actual generated fieldname in the environment. The field ties each seed record to its stable fixture identity.
4. Create or select an enabled seed user on that same ERPNext site. Give it read access to the company, referenced masters and GL, and create and submit rights for the relevant fixture documents. Generate an API key and secret under the user's API Access settings. Test the token with `/api/method/frappe.auth.get_logged_user` and confirm the expected user.
5. Record the exact ERPNext names for the company, groups, unit, group warehouse, accounts, and cost center. Discovery below can retrieve them. The company and these referenced records must exist before preflight. The loader creates P-WH1 and P-WH2.

### 2. Configure the PowerShell seed session

From the project root, install dependencies if needed. Run each port-forward in its own PowerShell window. Keep the forwards open while seeding and validating.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\source-setup\seed\requirements.txt
```

```powershell
kubectl --context kind-otc -n otc-sources port-forward svc/commerce 8000:8000
kubectl --context kind-otc -n otc-sources port-forward svc/postgres 15432:5432
kubectl --context kind-otc -n otc-sources port-forward svc/minio 9000:9000
kubectl --context kind-otc -n otc-erp port-forward svc/erpnext 18080:8080
```

#### Discover ERPNext master data before configuring the seed

Set ERP_URL, ERP_KEY, and ERP_SECRET in the local erp.env file first. Run the read-only discovery from the project root.

```powershell
.\.venv\Scripts\python.exe .\source-setup\seed\discover_erp.py
```

If the demo company is missing, complete the ERPNext setup wizard or create a dedicated INR demo company with its chart of accounts. An empty company list can also indicate that the seed user lacks permission. Run discovery again, then pass the exact company ID from its output.

```powershell
$Company = Read-Host 'Exact demo company name from discovery'
.\.venv\Scripts\python.exe .\source-setup\seed\discover_erp.py --company $Company
```

Use the returned record names for the `ERP_*` master-data settings. Review the account types and choose five distinct posting accounts. An empty option list means the relevant record must be configured or made visible to the seed user. A non-200 custom-field probe requires checking the exact fieldname and the token user's DocType permissions. Also verify fiscal dates and permissions below. Discovery uses GET requests and does not create company records. The seed creates the two leaf warehouses and the transactional fixtures after preflight.

#### ERPNext environment file

From the project root, copy the template if you do not already have an environment file.

```powershell
if (-not (Test-Path .\source-setup\seed\erp.env)) {
    Copy-Item .\source-setup\seed\erp.env.example .\source-setup\seed\erp.env
}
```

Save the following in `source-setup/seed/erp.env` with literal values and no surrounding quotes. The company and master record names below are examples for an INR company named **OTC Parent Demo** with abbreviation **OTC**. Copy actual record names from your site. Generate the key and secret in the seed user's **Settings → API Access** and keep them in your local file.

```dotenv
ERP_URL=http://127.0.0.1:18080
ERP_KEY=<ERPNext seed user's API key>
ERP_SECRET=<ERPNext seed user's API secret>
ERP_COMPANY=OTC Parent Demo
ERP_CUSTOMER_GROUP=Commercial
ERP_TERRITORY=India
ERP_ITEM_GROUP=Products
ERP_UOM=Nos
ERP_WAREHOUSE_GROUP=All Warehouses - OTC
ERP_WAREHOUSE_1=P-WH1
ERP_WAREHOUSE_2=P-WH2
ERP_BANK=Bank Account - OTC
ERP_AR=Debtors - OTC
ERP_INCOME=Sales - OTC
ERP_EXPENSE=Cost of Goods Sold - OTC
ERP_INVENTORY=Stock In Hand - OTC
ERP_COST_CENTER=Main - OTC
ERP_ORDER_REF_FIELD=custom_otc_seed_id
ERP_ALLOW_DEMO_WRITES=yes
```

| Setting | Required configuration |
|---|---|
| Company and fiscal year | Use a dedicated INR demo company with an open fiscal year covering 1–15 November 2026. Enable demo writes after confirming the target company. |
| Groups and units | Use existing Customer Group, Territory, Item Group, and UOM records. |
| Warehouses | Use an existing group warehouse for that company. The loader creates P-WH1 and P-WH2 under it. |
| Accounts and cost center | Use five distinct, active posting accounts from the company's Chart of Accounts and an active posting cost center. Align the company inventory account with ERP_INVENTORY. |
| Custom field | Add a unique Data field called OTC Seed ID with fieldname custom_otc_seed_id to Sales Order, Stock Entry, Delivery Note, and Sales Invoice. |
| API permissions and taxes | Give the seed user read access to masters and GL, create access to fixture records, and submit access for posted transactions. Use tax-free fixtures and run one seed writer at a time. |

The bundle also includes an interactive environment helper. With the URL and API credentials saved, select the exact name of the dedicated INR demo company and configure the discovered records:

```powershell
.\.venv\Scripts\python.exe .\source-setup\seed\populate_erp_env.py --company $Company --allow-demo-writes
```

#### PostgreSQL, Commerce, and MinIO environment values

Run this from the project root in the same PowerShell session that runs the loader. It reads the deployed Secret and sets the values without printing credentials.

```powershell
Get-Content .\source-setup\seed\erp.env -ErrorAction Stop | ForEach-Object {
    if ($_ -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}

$SecretJson = kubectl --context kind-otc -n otc-sources get secret source-credentials -o json
if ($LASTEXITCODE -ne 0) { throw 'Could not read source-credentials in otc-sources.' }
$SourceCredentials = ($SecretJson | ConvertFrom-Json).data

function Get-SourceCredential([string]$Name) {
    $Encoded = $SourceCredentials.$Name
    if (-not $Encoded) { throw "Missing Secret key: $Name" }
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Encoded))
}

$DbPassword = [Uri]::EscapeDataString((Get-SourceCredential 'POSTGRES_PASSWORD'))
$CommercePassword = [Uri]::EscapeDataString((Get-SourceCredential 'COMMERCE_PASSWORD'))
$env:DEALER_SEED_DSN = "postgresql://postgres:${DbPassword}@127.0.0.1:15432/dealer_legacy"
$env:COMMERCE_VERIFY_DSN = "postgresql://commerce:${CommercePassword}@127.0.0.1:15432/commerce"
$env:COMMERCE_URL = 'http://127.0.0.1:8000'
$env:S3_ENDPOINT = 'http://127.0.0.1:9000'
$env:INBOUND_BUCKET = 'otc-inbound'
$env:AWS_ACCESS_KEY_ID = Get-SourceCredential 'MINIO_ROOT_USER'
$env:AWS_SECRET_ACCESS_KEY = Get-SourceCredential 'MINIO_ROOT_PASSWORD'
$env:AWS_DEFAULT_REGION = 'us-east-1'

Remove-Variable SecretJson, SourceCredentials, DbPassword, CommercePassword
Remove-Item Function:\Get-SourceCredential
```

The dealer loader needs the database owner to add fixture columns and write rows. The Commerce verifier uses the Commerce role to read the order, POS receipt, and outbox.

### 3. Run preflight, then seed

Preflight checks connectivity, database names, ERP references, the inbound bucket and the Kafka topic. ERPNext checks document and submit rules during writes. If preflight finds the earlier one-unit smoke payload under P-E1, stop and use an isolated demo source stack with a separate database and topic instance. The full seed uses ten pumps at ₹10,000 each under the same event ID.

```powershell
$Python = '.\.venv\Scripts\python.exe'
& $Python .\source-setup\seed\load.py preflight
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed. Resolve the reported source prerequisite before loading.' }

foreach ($Step in @('erp', 'dealer', 'commerce', 'documents')) {
    & $Python .\source-setup\seed\load.py $Step
    if ($LASTEXITCODE -ne 0) { throw "Seed step failed: $Step" }
}
```

The ERP loader uses stable fixture keys and reuses valid documents on retry. The dealer step commits as a single transaction. The Commerce step posts the ten-pump P-S1 order and P-POS1 receipt. The outbox publishes one acceptance. Run the replay separately when testing duplicate delivery. The documents step publishes candidates and evidence, with four manual pair decisions pending.

The credit note requests that the original invoice's outstanding balance be updated when the credit is posted. If ERPNext leaves P-CR1 unallocated, use Payment Reconciliation to apply the existing ₹5,000 credit to P-I2, verify ₹35,000 outstanding, then rerun the ERP step. Check the existing documents before retrying.

### 4. Export and verify the seed

After the dealer load, create a new export Job. Use its Job name to select the exact snapshot in MinIO.

```powershell
$ExportJob = "dealer-export-$(Get-Date -Format 'yyyyMMddHHmmssfff')"
kubectl --context kind-otc -n otc-sources create job --from=cronjob/dealer-export $ExportJob
if ($LASTEXITCODE -ne 0) { throw 'Dealer export Job creation failed.' }

kubectl --context kind-otc -n otc-sources wait --for=condition=complete "job/$ExportJob" --timeout=180s
if ($LASTEXITCODE -ne 0) { throw "Dealer export did not complete: $ExportJob" }

kubectl --context kind-otc -n otc-sources logs "job/$ExportJob"
& $Python .\source-setup\seed\verify.py --snapshot $ExportJob
if ($LASTEXITCODE -ne 0) { throw 'Seed verification failed.' }
```

The verifier reads ERPNext documents and GL, both PostgreSQL databases, Kafka and MinIO. It prints JSON with counts and control totals. After one intentional Kafka replay, run the same verifier with `--deliveries 2`. P-E1 must have the same payload and two distinct offsets. Its timestamp is assigned by the live API.

| Source | Expected first-seed counts |
|---|---|
| Parent ERPNext masters | 5 customers, 5 items, 2 warehouses, 5 configured posting accounts |
| Parent ERPNext activity | 5 orders (1 submitted, 4 mapping drafts), 2 opening stock entries, 2 deliveries, 2 invoices, 1 credit note, 1 receipt, 1 cash allocation. GL count depends on ERP configuration. Verify balanced vouchers and ₹95,000 net posted revenue. |
| Dealer PostgreSQL | 5 customers, 5 products, 1 warehouse, 5 accounts, 5 orders, 5 order lines, 1 shipment, 1 storefront order, 1 storefront status, 1 invoice, 1 AR snapshot row, 1 receipt, 1 allocation, 4 GL lines, 1 POS receipt with 1 line and 1 tender |
| Parent commerce PostgreSQL and Kafka | 1 order, 1 published outbox record, 1 POS receipt. Kafka has 1 logical acceptance event and 1 initial delivery, or 2 after intentional replay. |
| MinIO | 11 dealer CSVs with 18 rows total, 4 Finance CSVs with 4 rows, 6 MDM and policy objects, and 4 manifests for the selected snapshot and fixture version |
| MDM | 10 candidate decisions across customers and products, with 4 automatic, 4 pending, and 2 separate cases. Five account crosswalks per company. Expect six group customers and six group products after review. |

For manual inspection, open the ERPNext document IDs listed in `erp-seed-receipt.json`, read POS with `curl.exe -fsS http://127.0.0.1:8000/pos/receipts`, and use the export Job log to locate the exact MinIO manifest. The verifier checks the listed objects with SHA-256 and CSV row counts. Metro should show ₹135,000 net invoiced and posted revenue, ₹35,000 cash applied, and ₹100,000 open AR across the two sources. Group reporting still requires ingestion and approved crosswalks.

The ten-order fixture can be checked independently of the three-month expansion. Source verification requires live access to your ERPNext site and kind cluster.
