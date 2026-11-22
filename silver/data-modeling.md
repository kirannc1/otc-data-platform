# Data modeling for the order-to-cash platform

Silver integrates source records into a consistent business model. Gold exposes facts and dimensions for Sales and Finance. For this project, start with **normalized canonical tables in Silver** and use **Kimball dimensional modeling in Gold**. Data Vault is an alternative design for the Silver integration layer where preserving source-specific observations and their history is a priority.

## Canonical and conformed modeling

| Concern | What it defines | Project example |
|---|---|---|
| Canonical structure | A common shape and meaning for the same business record across sources | Map ERPNext’s nested sales order and the dealer’s separate order tables into `sales_order` and `sales_order_line`. Preserve source identifiers. |
| Conformance | Shared business identities and definitions for reporting across datasets | Map verified parent and dealer SKUs to the same product identity. Sales and Finance can then use a shared Product dimension. |

A dealer order can enter the canonical model while its product mapping is pending. Keep the scoped source SKU and mapping status until the equivalence is approved.

The model covers master data such as customers, products, warehouses, and legal entities, along with order-to-cash transactions, supporting channel and inventory records, and reference codes. Across all of these, document the grain, keys, ownership, source mappings, lineage, and validation rules.

## Silver with Inmon-style normalized tables

Inmon’s Corporate Information Factory uses an integrated enterprise warehouse with detailed normalized records that feed downstream marts. The following is a practical design sequence for our Silver layer. [Architecture reference](https://catalogimages.wiley.com/images/db/pdf/9781118530801.excerpt.pdf)

1. **Identify business entities and declare their grain.** Define one row per customer, product, sales order, and sales order line.
2. **Identify keys and relationships.** A sales order belongs to a legal entity, may reference a customer, and can have multiple lines. Each line belongs to an order and references a product identity where resolved.
3. **Normalize attributes.** Give each line its own row for 1NF. Move attributes that depend only on the order portion of the line key to the order header for 2NF. Keep current customer and product attributes on their respective entities for 3NF. Retain ordered quantity and agreed unit price on the line.
4. **Define source integration and history.** Map both sources into the shared schema. Scope source IDs by system and legal entity where needed. Apply approved crosswalks and document how to retain changes such as order status or point-in-time receivables.

**Example.** Order `O100`, line `1`, has two pumps at an agreed unit price of ₹10,000. The `sales_order_line` record holds the quantity and agreed price. The order date lives on `sales_order`, and the current product name lives on `product`.

## Silver with Data Vault

Data Vault structures the integration layer around business keys, relationships, and descriptive observations. Source-specific history remains traceable. [Data Vault constructs](https://www.scalefree.com/knowledge/webinars/expert-sessions/coalesce-transformation-talks/creating-data-vault-stages/)

1. **Identify business keys for Hubs.** Register Order, Customer, and Product identities. Scope keys when the same source identifier can refer to different business entities.
2. **Identify associations for Links.** Connect the order to its customer. One possible design represents ordered products through an order-line Link at line grain. Keep line `1` distinct from another line with the same SKU.
3. **Assign descriptive attributes and history to Satellites.** Store quantity and agreed price for the line relationship, and status for the order. Record the source and load timestamp. Where available, retain the business-effective timestamp separately.
4. **Apply business rules for consumption.** Load source observations into the Raw Vault. Use the Business Vault for rules such as an approved Same-As Link between parent and dealer product identities. Expose business-ready records to downstream marts. [Business-key equivalence](https://www.scalefree.com/knowledge/webinars/data-vault-friday/standardizing-business-keys/)

**Example.** `O100` keeps the same Hub identity when its status changes from `OPEN` to `SHIPPED`. A Satellite records the later observation alongside the earlier one. A reporting view can select the version known at a given cutoff.

## Gold with Kimball dimensional modeling

Kimball’s four steps apply to each business process. Declare the grain before choosing dimensions and measures. [Four-step design](https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/four-4-step-design-process/)

| Step | Order booking example |
|---|---|
| Select the business process | Sales order booking |
| Declare the grain | One row per booked sales order line |
| Identify the dimensions | Order date, customer, product, legal entity, and channel where known |
| Identify the facts | Ordered quantity and booked amount. For `O100`, line `1`, these are 2 units and ₹20,000. |

The resulting `fact_sales_order_line` represents bookings. Shipment and billing facts have their own grains and can reuse conformed dimensions such as Product. Booked amount and net invoiced amount remain distinct measures. [Grain](https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/grain/) · [Conformed dimensions](https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/conformed-dimension/)

## First project slice

Declare the grain, keys, and source-to-target rules for `sales_order` and `sales_order_line`. Map one ERPNext order and one dealer order into normalized Silver, preserving their source keys and mapping status. Reconcile each order and its lines to Bronze before applying Kimball’s four steps to the first Gold sales fact.

## Agreed modeling sequence

Use normalized, Inmon-style tables for the Silver implementation and Kimball facts and dimensions for Gold. Start with grain and business keys, then document relationships and cardinality, normalize attributes, and define source mappings and history. Iterate when source profiling shows that a key or relationship needs a different scope. Data Vault remains an integration design option if source-specific change history becomes a more central requirement.

MDM starts in parallel with Silver. Bronze supplies the source masters for profiling. Silver can load canonical transactions while matches are under review, then attach shared identities after approvals. Gold uses those identities for cross-company rollups and makes the unmapped portion visible.

## Silver entity inventory and proposed grain

The following grains are the logical design. Confirm source identifiers, fields, and link coverage before turning the design into physical tables.

| Group | Entity | One row represents |
|---|---|---|
| Master | `legal_entity` | One company that books transactions. The parent and acquired dealer retain separate identities. |
| Master | `customer`, `product` | One shared buyer identity or one shared product identity, respectively. |
| Master | `user` | One account in its source system, where actor data is captured. A cashier, salesperson, or authenticated buyer can be recorded in the relevant role. |
| Master | `warehouse`, `gl_account` | One inventory location or one posting account, each belonging to a legal entity. |
| Pricing | `price_list` | One pricing schedule for a selling legal entity. |
| Pricing | `price_list_item` | One price for a product on a list, in a currency and unit of measure, for an effective period. Add a quantity tier if the source supplies one. |
| Transaction | `sales_order`, `sales_order_line` | One sales order or one distinct line on that order. |
| Transaction | `shipment`, `shipment_line` | One shipment or one item line on that shipment. |
| Transaction | `billing_document`, `billing_document_line` | One invoice or credit note, or one item or charge line on that document. |
| Transaction | `payment`, `payment_allocation` | One receipt of payment, or one application of a payment amount to an invoice. |
| Transaction | `gl_posting_line` | One debit or credit posting to a GL account. |
| Snapshot | `receivables_snapshot` | One invoice’s open balance at a reporting cutoff. Include the reporting source if retaining multiple observations per invoice and cutoff. |
| Channel | `order_event` | One logical storefront event, such as an order acceptance. |
| Channel | `pos_receipt`, `pos_receipt_line` | One finalized counter receipt or one item line on that receipt. |
| Inventory | `inventory_movement_line` | One physical stock movement line for a product at a warehouse. |
| Reference | Currency, unit, channel, event, document, status, and payment codes | One code in a defined domain. Preserve original source codes alongside standardized meanings where needed. |

An order has many lines. An order line can be fulfilled through multiple shipment lines. A billing line can link back to the order line or shipment line where the source provides a reliable reference. A credit note can reference its original invoice. Payments and invoices can have a many-to-many relationship, so the allocation records the amount applied to each invoice. A financial credit can reduce billing and open receivables while the physical shipped quantity remains unchanged.

### Pricing and transaction history

Pricing is separate from Product. A product can appear on multiple price lists, at different prices and units, across selling companies and effective periods. Customer-specific contract pricing belongs to the applicable commercial agreement. Add price-list or agreement tables to the implementation when source records are available.

Keep the agreed unit price, recorded quantity, unit, currency, and any source discount on the order line. Keep billed prices and signed invoice or credit amounts on the billing records where source detail supports them. For example, a list price of ₹10,500 and an agreed order price of ₹9,500 describe different facts. A later list-price change leaves the historical order price intact.

### Keys, history, and metadata

- **Stable identity.** Preserve source system, legal entity, and source record ID. For a line number unique only within its document, include the parent document identity in the source key. Store canonical IDs where resolved. Keep the parent and dealer warehouses distinct.
- **Time.** Separate business-effective time from ingestion time. Document which changes need history, such as order status or approved crosswalk changes. Record receivables at an explicit cutoff.
- **Metadata.** Record grain, key, required fields, ownership, mapping rules, and quality tests in dataset contracts. Keep source identifiers and Bronze provenance available for record-level tracing. Capture input and output counts, validation results, and the mapping version in run metadata.

## MDM in parallel with Silver

1. **Profile source masters.** Inspect parent and dealer customer codes, product SKUs, duplicates, units, pack sizes, and gaps using Bronze records.
2. **Define matching rules and review exceptions.** Keep candidate matches available for review. Approve shared identities only when the evidence supports them. Include one approved and one pending case in the first test.
3. **Publish versioned crosswalks and conversions.** Key crosswalks by source system, legal entity, and source ID. Record approval status and applicable effective periods or versions. Keep aliases, quantity conversions, and commercial terms explicit.
4. **Apply approved mappings.** Enrich affected Silver rows and refresh dependent Gold outputs when approval changes a rollup. Preserve the original source key, quantity, and unit. Keep pending matches visible in reporting.

| Mapping or rule | Key or scope | Example |
|---|---|---|
| Customer or product identity | Source system, legal entity, and source customer ID or SKU | A verified parent and dealer SKU map to the same shared pump. |
| Product unit conversion | Product or source SKU, from-unit, to-unit, and applicable period | One dealer pack contains two pumps. |
| Customer-specific alias or ordering unit | Customer, product, source or agreement context as needed, and applicable unit and period | A buyer calls the pump `SKU-42`, or orders it in an agreed case size. A quantity conversion requires the approved customer, product, and unit combination. |
| Commercial terms | Applicable price list or customer agreement | A negotiated price or payment term applies for the agreed period. The actual order price remains on the order line. |

## Apply the three approaches to the same order

| Design concern | Inmon-style Silver | Data Vault-style Silver | Kimball Gold |
|---|---|---|---|
| Order and line | Order header and normalized order line with business attributes and foreign keys | Hubs for stable keys, Links for associations at their declared grain, Satellites for descriptive observations | A bookings fact at one row per booked order line, joined to shared dimensions |
| Order status changes | Define current state and explicit version or status history where required | Retain source observations in Satellites and select the version relevant to the report | Expose status or lifecycle measures according to the fact’s grain and business definition |
| Parent and dealer SKUs match | Apply the approved mapping to the shared Product ID | Retain source identities in the Raw Vault and express approved equivalence in the Business Vault | Reuse a conformed Product dimension across booking, shipment, and billing facts |

The first implementation slice is **Legal Entity, Customer, Product, Warehouse, Sales Order, and Sales Order Line**. Declare their grain and scoped source keys, document the relationships, then map one parent order and one dealer order. Reconcile the order IDs, line counts, quantities, and booked amounts to Bronze. Verify that an approved product match rolls up under the shared identity and that the pending record remains traceable. Extend the design to fulfillment, billing, and settlement once this slice is reconciled.

## OTC Silver logical design and review sequence

Use this section to review the full parent-and-dealer model before implementing the Silver notebooks. The operating companies are the accounting entities. The private equity sponsor is ownership context and does not receive either company's transactions. Start with the Bronze inventory and pinned manifests from the validated ingestion runs. Record the source fields, keys, coverage, and exceptions that support each modeling decision.

### 1. Declare the grain of every entity

| Area | Silver entity | One row represents | Bronze evidence |
|---|---|---|---|
| Master | `legal_entity` | One operating company | ERPNext Company for the parent. Configured dealer database and entity registry entry for the dealer. |
| Master | `source_customer`, `source_product` | One customer account or sellable item in its source scope | ERPNext Customer and Item, dealer SQL customers and products |
| Master | `group_customer`, `group_product` | One governed identity used for cross-company reporting | Approved MDM decisions derived from source masters and stewardship evidence |
| Master | `warehouse`, `gl_account` | One location or local chart-of-accounts entry within a company | ERPNext Warehouse and Account, dealer SQL warehouses and accounts. Keep group or control nodes distinguishable from operational warehouses and posting accounts. |
| Master | `bank_account`, `source_user` | One bank account in a company or one actor account within a source | Bank account identity comes from an approved registry and statement metadata. User master capture is outside the current Bronze contract. |
| Reference | `reference_code` | One code in a domain such as currency, UOM, status, channel, or tender | Source codes and the governed code mappings. ERPNext UOM is captured. |
| Reference | `reporting_account_category` | One category in the group reporting chart | Finance-owned chart and approved account mappings |
| Pricing | `price_list`, `price_list_item` | One price schedule, or one effective product price on a schedule for a currency, UOM and applicable tier | Design only until catalog prices or agreements are captured. Transaction prices are already available where the source records them. |
| Order | `sales_order`, `sales_order_line` | One order header, or one distinct item row on that order | ERPNext Sales Order and nested items, dealer SQL sales_orders and order_lines |
| Fulfillment | `shipment`, `shipment_line` | One dispatch document, or one item row dispatched | ERPNext Delivery Note and items. Dealer SQL currently provides one shipment item per shipment ID. |
| Returns | `return_document`, `return_line` | One physical return document, or one returned item row | Return-marked delivery records where present, dealer SQL returns. A financial price allowance follows the billing grain. |
| Billing | `billing_document`, `billing_document_line` | One invoice or credit memo, or one item or charge on it | ERPNext Sales Invoice and items, dealer invoice CSV. The current dealer export provides document amounts. |
| Settlement | `payment`, `payment_allocation` | One payment or receipt, or one application of a payment to a billing document | ERPNext Payment Entry and references, dealer receipt and allocation CSVs |
| Accounting | `gl_posting_line` | One debit or credit posting to a local account | ERPNext GL Entry, dealer GL CSV |
| Snapshot | `receivables_snapshot` | One invoice open balance per cutoff and balance source | Dealer AR CSV. Parent as-of balances use dated accounting evidence, subject to coverage and allocation checks. |
| Inventory | `inventory_movement_line` | One signed stock movement leg for one product at one location | ERPNext Stock Entry items and actual stock-affecting fulfillment documents when integrated. A transfer produces an outbound and an inbound leg. |
| Channel | `order_event` | One logical storefront event | Parent Kafka, dealer order-status export |
| Channel | `pos_receipt`, `pos_receipt_line`, `pos_tender` | One receipt, one receipt item, or one tender applied to the receipt | Parent Commerce REST and dealer POS CSVs. Preserve the parent tender code and populate an amount only with source evidence. |
| Banking | `bank_statement_line`, `bank_match_allocation` | One statement transaction, or one approved amount matched from it to a payment or GL posting | Finance CSV and derived reconciliation decisions |
| Planning | `budget_amount` | One approved amount per company, period, scenario/version, currency and planning scope | Finance CSV. The parent fixture plans by product category and the dealer by account. |
| MDM | `match_candidate`, `identity_crosswalk`, `uom_conversion` | One candidate decision case, one effective source-to-group identity mapping, or one approved unit conversion for a defined scope and period | MinIO seed evidence and approved stewardship output |
| Control | `source_observation`, `model_run`, `quality_exception` | One lineage link from a modeled row to Bronze evidence, one execution, or one rule failure per input key and run | Bronze run IDs, manifest keys, hashes, Kafka coordinates, rule versions and outcomes |

### 2. Define business keys, relationships, and coverage

The source key for a document or master is its native ID within the source installation and legal-entity scope. Retain ERPNext `name` as the native document ID and `custom_otc_seed_id` as a fixture lookup or explicit channel bridge. ERPNext Customer and Item are site-wide masters. Preserve their site-native identities and record the company context where they are used. The dealer records use their PostgreSQL IDs. File delivery version and DAG run ID identify observations of a record. Use the same business identity across repeated observations of that record.

| Relationship | Key and cardinality | Integrity rule for this project |
|---|---|---|
| Company to order, account, warehouse, and posting | One company to many source records | Review the ERP company and dealer registry mappings. Validate company-scoped foreign keys on every document. |
| Customer to order to line | One source customer to many orders. One order to many lines. | Line key includes the order key and native child row ID. Product key is required for merchandise lines. Scope customer and product references to their source identity. |
| Order line to shipment line | One order line may have multiple shipment lines | ERPNext uses `against_sales_order` and `so_detail`. Dealer shipment supplies order and product. Establish a line link only with an explicit ID or a verified unique candidate. Retain the order link and flag ambiguous line coverage. |
| Shipment line to warehouse | Many shipment lines may use the same warehouse | Use the actual dispatch location. The warehouse recorded on an order is the planned location. Keep the two parent warehouses and one dealer warehouse distinct. |
| Shipment or order line to billing line | Billing can split across documents or combine fulfilled quantities | Preserve ERPNext `so_detail` and `dn_detail` where supplied. Keep dealer billing at document grain until line evidence arrives. |
| Original invoice to credit | One invoice may have several credits | ERPNext uses `return_against` and the source line reference when present. Dealer documents provide order and document type. Apply a credit to an invoice only with explicit evidence or a governed decision. |
| Payment to invoice | Many-to-many through payment allocation | Use native allocation or payment-reference row ID. Preserve unallocated cash separately. Aggregate allocations at invoice grain before joining to invoices. |
| Voucher to GL posting | One voucher may generate several postings | Scope by company, voucher type and source voucher ID. Dealer uses the document ID. Compare voucher totals before joining to order or billing detail. |
| Order to event and POS | An order can have many events. A receipt has many lines and potentially several tenders. | Deduplicate on scoped logical event ID and retain topic, partition and offset as delivery evidence. Link channel and ERP only through an explicit reference. A POS receipt needs evidence of an ERP link before combining it with an ERP booking. |
| Invoice to AR observation | One invoice to many cutoffs | Include cutoff and source in the key. Use one agreed cutoff for comparisons. |
| Account to statement and match | One account to many statement lines. Matching can be many-to-many. | Use bank account plus transaction ID within source. Store matched amount, method, date and status. Distinguish event time, booking date and bank value date. |
| Product to stock movement | One product to many movement legs | Key each leg by document type, source document and line ID, plus leg direction or native movement ID. Match stock-affecting fulfillment to its source movement once. |
| Product to price or conversion | One product to many effective prices or conversion rules | Price item key includes price list, product, currency, UOM, tier and effective start. Conversion scope includes source or agreement where applicable, from/to UOM and effective start. Review overlapping effective windows. |
| Planning scope to budget | One planning scope to many periods and versions | Key by company, scope type and scope ID, period, currency and scenario/version. Document the approved default scenario when the source omits it. Aggregate actuals to the matching budget scope before comparison. |

### 3. Normalize and standardize the canonical records

1. **Flatten at source grain.** Split nested ERPNext items and payment references into child records. Split POS tenders from receipt items. Keep one row per atomic item, posting, allocation, or reference and keep its original identifier. Keep missing detail coverage explicit.
2. **Move attributes to their owning entity.** Customer, currency and order date belong to the order header. Item, commercial quantity, UOM, warehouse reference and line amount belong to the line. Customer and product descriptions live with their master identities. Account hierarchy and reporting category belong to the chart and its crosswalk.
3. **Keep business quantities separate.** Ordered, shipped, returned, and billed quantities represent distinct facts. Keep recorded transaction prices and amounts. Use decimal arithmetic and convert the dealer's INR paise to rupees with the source contract. Keep a missing dealer unit price nullable. Capture document, company and account currencies separately where supplied.
4. **Standardize statuses and dates with explicit rules.** Retain raw status, ERPNext document state, and source timestamps. Define booked, draft, cancelled and unknown using source evidence. Store date-only business dates without timezone conversion and normalize timestamped events to UTC with their original offset available. Keep missing, zero and derived values distinguishable.
5. **Introduce conformed references using approved decisions.** Canonical records carry local customer, item, account and location keys. Conformed views or links attach effective group IDs and approved converted quantities. Price lists and buyer aliases remain distinct from Product and from actual transaction prices.

### 4. Reconcile to the sources and to financial balances

Pin the REST and SQL run IDs, select named file manifests and cutoffs, and record the Kafka offset range. A successful DAG run proves execution. The checks below establish what arrived and whether modeling preserved its business meaning.

| Control | Expected evidence and comparison |
|---|---|
| Bronze completeness | Verify manifests, checksums and collection counts. For each dataset, account for every source row at its declared grain as accepted, rejected, or a duplicate under a documented rule. For nested collections, reconcile child counts independently of header counts. |
| Relationships and coverage | Check unique scoped keys and required references. Report missing source fields, late-arriving parents, ambiguous line references, and document-only coverage separately. Show source ID, Bronze key and the rule in every exception. |
| Order and shipment | P-S1 books 10 pumps and ₹100,000. Its two delivery lines ship 6 and 4 units from separate warehouses. D-S1 books one pump and ₹40,000, and D-DEL1 ships one. Keep order, shipment, and billing measures separate. Compare like UOMs. |
| Billing to GL | For the Metro baseline, parent gross invoices total ₹100,000 and its ₹5,000 credit brings net billing to ₹95,000. Dealer net billing is ₹40,000. Compare posted revenue credits less debits within the same company, currency and period using approved revenue accounts. Expected baseline is ₹135,000 net billing and ₹135,000 posted revenue. Check balanced debits and credits per voucher within its company currency. Capture each seeded discrepancy as an exception with its expected cause. |
| Cash and AR | Parent P-P1 applies ₹25,000 to invoice A, leaving ₹35,000. The credit against B leaves ₹35,000. Dealer D-R1 applies ₹10,000, leaving ₹30,000 on D-I1. At the 10 December fixture cutoff, expected Metro AR is ₹100,000. Rebuild balances from dated invoices, credits, allocations and reversals through the cutoff. Reconcile to a contemporaneous source snapshot where available. Keep account-level net AR and invoice-level allocation exceptions visible when credit allocation evidence is incomplete. |
| Bank, POS, and streaming | Compare bank lines P-B1 and D-B1 to payment evidence by company, bank account, reference and amount. Reconcile POS items and tenders when both amounts are supplied. Count P-E1 once as an event if replayed, while retaining each delivery. |
| Round two | Compare the confirmed pre- and post-seed manifests at the relevant grains. Confirm added orders, shipments, invoices, credits, GL rows, dealer AR export, and event. Confirm unchanged master snapshots. Each company adds a two-unit shipment, a ₹20,000 invoice and a ₹10,000 credit. Expect ₹10,000 incremental net billing and posted revenue per company. Validate the parent’s ₹10,000 invoice balance and the dealer’s ₹10,000 AR row at its 16 December cutoff. Keep the dealer’s 10 December baseline snapshot separate. |

Use exact fixture controls at the source currency precision. For each exception retain expected value, observed value, difference, source identifiers and reason. A missing allocation, missing export or incomplete cutoff is a coverage issue and carries its own status.

### 5. Define history and replay behavior

| Record type | History rule |
|---|---|
| Orders, masters and operational documents | Retain key-stable observations or versions when modeled fields change. Maintain a reproducible current view. An unchanged row in a fresh snapshot must leave business history at the same version. Preserve cancellations and amendments explicitly. |
| Billing, payments and GL | Preserve original signed postings and the credit, reversal or adjustment as separate records. Keep posting and observation dates. As-of balance calculations use posting and allocation dates and disclose gaps in allocation history. |
| AR and budgets | Preserve each cutoff or published version. A revision has its own observation lineage. Historical comparisons identify which export or plan version was used. |
| MDM and conversions | Preserve effective-from/to, approval time and recording time. Close or supersede an old version and keep approval evidence. Record the mapping version used to reproduce a past result or intentionally restate it. |
| Kafka and files | Preserve raw deliveries and file manifests in Bronze. Use logical keys and payload evidence to recognize replays. Hold conflicting payloads for the same logical event ID for review. |

Where a source supplies a business-effective timestamp, retain it alongside Bronze observation time. For snapshots with no effective timestamp, label the change as first observed and avoid inventing an effective date. Treat a missing row in a later snapshot as an absence observation until completeness and source deletion semantics are confirmed. The design review should show that running the same inputs and rules twice produces the same canonical results, and that late records can be replayed without duplicate bookings or postings.

### 6. Run MDM in parallel with the Silver design

| Phase | Rule for this use case | Output |
|---|---|---|
| Profile | Inventory native customer IDs and observed aliases, registration IDs, names, item codes, manufacturer and part numbers, UOMs, pack sizes, locations, and account types. Measure blanks, duplicates, uniqueness and transactional reference coverage per company. | Source profile and a field-level evidence matrix |
| Standardize | Preserve raw values. Normalize case and whitespace for comparisons, retain leading zeros in identifiers, and validate country or registry scope for registration IDs. Standardize manufacturer aliases, part-number formatting and UOM codes via approved dictionaries. Preserve packaging, legal form and meaningful ID characters. | Comparable fields with rule version and source lineage |
| Generate candidates | Match customers on a verified registration identifier within the same registry or equivalent approved evidence. For products, compare verified manufacturer and part identifier with compatible attributes. Use name similarity or a partial product key to suggest review. | Candidate pairs, rule, supporting attributes, conflicting attributes and confidence reason |
| Decide and resolve | Auto-approve only a unique deterministic match with compatible evidence and no conflict. Queue missing identifiers, contradictory evidence, one-to-many candidates, or uncertain pack sizes. The responsible steward approves a match, requests evidence, or approves distinct identities with a reason. | Approved, pending, rejected, or confirmed distinct decisions |
| Choose authoritative attributes | Use verified registration evidence for customer identity, verified catalog evidence for product identity and pack size, and Finance-approved mappings for account categories. Keep local trading names, local SKUs, warehouse names and codes on the source records. | Group records and field-level survivorship decisions |
| Publish and apply | Publish immutable decision versions with source scope and key, group ID when approved, effective period, approver, rule version and evidence pointer. Permit at most one active approved identity per source record at a given effective time. Apply the relevant version to transactions and show the pending population. | Versioned crosswalk, conversion rules, unmapped coverage and auditable rollups |
| Correct and replay | Require review for a merge, split, changed factor or correction. Retain old and new mappings and their reason. Identify affected records and rerun dependent outputs with the new version where restatement is approved. | Change history and reproducible reconciliations |

#### Decisions to exercise with the seeded data

| Domain | Scenario | Expected treatment |
|---|---|---|
| Customer | Metro and Pioneer | Two approved matches using verified shared registration evidence. Confirm the bridge between ERPNext Customer `name` and each fixture alias before using the candidate file. |
| Customer | Apex and Summit | Queue the name similarities when acquired registration evidence is missing. Review registration and corroborating evidence. A conflicting verified registration ID holds the merge until resolved. |
| Customer | Harbor and Valley | Retain distinct approved group identities. One company may have more than one local account mapping to a group customer only when the account-to-identity relationship is verified. |
| Product | Pump and valve | Approve both manufacturer-and-part matches with compatible units. Preserve each company's SKU. |
| Product | Parent P-FILT and dealer D-FLT | Manufacturer is missing in the dealer record. Verify ACME F200 against the supplier evidence before approval. |
| Product | Parent P-SEAL and dealer D-SEAL | Establish shared product identity and approve the pack conversion separately. After the conversion is approved, 1 dealer pack reports as 10 each. Keep quantity 1 pack and its recorded line amount on the source transaction. |
| Product | Parent gauge and dealer hose | Assign distinct identities after review. |
| Warehouse | P-WH1, P-WH2 and D-WH1 | Keep three identities under their owning companies. A similar label alone never establishes a shared site. |
| GL account | Local Sales, Receivables, Bank, Inventory and COGS | Finance approves the effective mapping to group reporting categories. Dealer `400-D` maps to Sales Revenue. If the category is disputed, preserve the posting and flag the reporting mapping. Never use a reporting category as the local account identity. |
| Bank account | ERP account, payment destination and bank statement | Finance verifies the legal entity, bank account reference, currency and effective dates. Keep GL account mapping and proof of bank account identity as separate relationships. |
| Customer-specific alias or ordering unit | A buyer-specific SKU or contract ordering unit | Key the alias or conversion by customer, product, applicable source or agreement, unit and effective period. An approved buyer-specific rule governs that scope. Block conversion for overlapping or contradictory applicable rules. Preserve source quantity, unit, amount and the chosen conversion version. |

The fixture starts with two approved pairs, two pending pairs and two distinct local records per domain. Six group customers and six group products are expected after both pending pairs in each domain are approved. Identity approval and conversion approval are independent. A customer or product under review remains available at its local grain with its mapping status. When group reporting begins, surface those rows and amounts under unresolved coverage.

### 7. Review the design before opening the implementation notebooks

1. **Review the grain and key register** against the captured Bronze schema. Confirm field presence, document state, source scopes and coverage gaps.
2. **Review the relationships and canonical rules** using the Metro order, split shipment, invoices, credit, payment and GL postings. Check the dealer's document-level limits and agree how ambiguity is surfaced.
3. **Review balances and time rules** using the baseline cutoff, a no-change replay, and the round-two inputs. Agree which totals and evidence qualify a record or dataset for publication.
4. **Review MDM decisions** with customer, catalog and Finance ownership. Confirm the approved and pending fixture cases, effective dating, and the pack conversion test.
5. **Implement in slices after review.** Begin the notebooks with master identities and order header/line. Continue with fulfillment, billing and settlement using the agreed relationships and controls.
