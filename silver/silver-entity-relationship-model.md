# Order-to-cash Silver entity relationship model

This logical model covers the parent ERPNext company, the acquired dealer, and the channel and finance feeds. Each entity box states **what one row represents** and shows the keys needed to understand its joins. Read the diagrams as views of the same model. Repeated boxes represent the same entity.

- **PK** is the logical row identifier shown in the diagram. Scoped native source keys are retained as alternate keys. Scope business documents by source installation and company, and site-wide ERPNext Customer and Item masters by source site. A child’s native ID is scoped by its parent where necessary.
- **FK** points to another modeled entity. Some FKs are shown in a different view. Each relationship shows how many rows at one end may relate to one at the other. Nullable source links remain visible as coverage exceptions.
- **Grain** is written in the box title after the entity name. These diagrams define logical keys and relationships. Precisions, physical indexes, and the history storage mechanism belong in the implementation design.

```mermaid
flowchart TD
    M["Companies and source masters"] --> O["Orders, fulfillment and channels"]
    O --> F["Billing, payments and accounting"]
    M --> D["MDM crosswalks and reference data"]
    D --> F
    F --> B["Bank reconciliation and planning"]
    M --> C["Lineage and quality controls"]
    O --> C
    F --> C
    B --> C
    D --> C
```

## 1. Order, customer and product

```mermaid
erDiagram
    direction TB
    LEGAL_ENTITY["Legal Entity · one operating company"] {
        id legal_entity_id PK
    }
    SOURCE_CUSTOMER["Source Customer · one source customer account"] {
        id source_customer_id PK
        key native_customer_key
    }
    SALES_ORDER["Sales Order · one order in a source company"] {
        id sales_order_id PK
        id legal_entity_id FK
        id source_customer_id FK
        date order_date
        code currency
        code order_status
    }
    SALES_ORDER_LINE["Sales Order Line · one item line on an order"] {
        id sales_order_line_id PK
        id sales_order_id FK
        id source_product_id FK
        id planned_warehouse_id FK
        decimal quantity
        code order_uom
        decimal line_amount
    }
    SOURCE_PRODUCT["Source Product · one source sellable item"] {
        id source_product_id PK
        key native_item_key
    }
    LEGAL_ENTITY ||..o{ SALES_ORDER : books
    SOURCE_CUSTOMER o|..o{ SALES_ORDER : places
    SALES_ORDER ||..o{ SALES_ORDER_LINE : contains
    SOURCE_PRODUCT ||..o{ SALES_ORDER_LINE : identifies
```

Customer identity resolves at the header. Product, ordered quantity, transaction unit and recorded amount belong to the line. A missing customer master may leave the resolved FK empty while retaining the source reference. A product FK is required on accepted merchandise lines. Draft or partially captured headers may have no lines. ERPNext fixture aliases such as P-S1 are lookup references.

## 2. Fulfillment and warehouses

```mermaid
erDiagram
    direction TB
    SALES_ORDER["Sales Order · one order in a source company"] {
        id sales_order_id PK
        id legal_entity_id FK
        id source_customer_id FK
        date order_date
        code currency
        code order_status
    }
    SALES_ORDER_LINE["Sales Order Line · one item line on an order"] {
        id sales_order_line_id PK
        id sales_order_id FK
        id source_product_id FK
        id planned_warehouse_id FK
        decimal quantity
        code order_uom
        decimal line_amount
    }
    SHIPMENT["Shipment · one dispatch document"] {
        id shipment_id PK
        id legal_entity_id FK
        id sales_order_id FK
    }
    SHIPMENT_LINE["Shipment Line · one item row dispatched"] {
        id shipment_line_id PK
        id shipment_id FK
        id sales_order_line_id FK
        id source_product_id FK
        id warehouse_id FK
        decimal shipped_quantity
    }
    WAREHOUSE["Warehouse · one company inventory location"] {
        id warehouse_id PK
        id legal_entity_id FK
        key native_warehouse_key
    }
    SALES_ORDER o|..o{ SHIPMENT : references
    SHIPMENT ||..o{ SHIPMENT_LINE : contains
    SALES_ORDER_LINE o|..o{ SHIPMENT_LINE : fulfilled_by
    WAREHOUSE o|..o{ SHIPMENT_LINE : dispatches
    WAREHOUSE o|..o{ SALES_ORDER_LINE : planned_for
```

A parent order line can ship in several deliveries. ERPNext provides the line reference. The dealer currently supplies order and product and can link to a line when the candidate is unique and verified. For shipments spanning orders, the line references carry order provenance. The warehouse on the order line is planned. The warehouse on the shipment line is actual.

## 3. Physical returns and inventory movements

```mermaid
erDiagram
    direction TB
    SHIPMENT_LINE["Shipment Line · one item row dispatched"] {
        id shipment_line_id PK
        id shipment_id FK
        id sales_order_line_id FK
        id source_product_id FK
        id warehouse_id FK
        decimal shipped_quantity
    }
    RETURN_DOCUMENT["Return Document · one physical return document"] {
        id return_document_id PK
        id legal_entity_id FK
    }
    RETURN_LINE["Return Line · one returned item row"] {
        id return_line_id PK
        id return_document_id FK
        id shipment_line_id FK
        id source_product_id FK
        decimal returned_quantity
    }
    WAREHOUSE["Warehouse · one company inventory location"] {
        id warehouse_id PK
        id legal_entity_id FK
        key native_warehouse_key
    }
    INVENTORY_MOVEMENT_LINE["Inventory Movement Line · one signed product movement at a location"] {
        id movement_id PK
        id legal_entity_id FK
        id source_product_id FK
        id warehouse_id FK
        key source_document_line_leg
        decimal signed_quantity
    }
    RETURN_DOCUMENT ||..o{ RETURN_LINE : contains
    SHIPMENT_LINE o|..o{ RETURN_LINE : returned_from
    WAREHOUSE ||..o{ INVENTORY_MOVEMENT_LINE : records
```

Return lines reference the original shipment line where evidenced. Each movement links to one source product and warehouse. Preserve the source movement reference for deduplication. A transfer yields two signed warehouse legs. A financial credit is modeled in Billing. A dealer return that lacks line evidence stays traceable through its source shipment reference.

## 4. Invoices and credits

```mermaid
erDiagram
    direction TB
    SALES_ORDER["Sales Order · one order in a source company"] {
        id sales_order_id PK
        id legal_entity_id FK
        id source_customer_id FK
        date order_date
        code currency
        code order_status
    }
    SALES_ORDER_LINE["Sales Order Line · one item line on an order"] {
        id sales_order_line_id PK
        id sales_order_id FK
        id source_product_id FK
        id planned_warehouse_id FK
        decimal quantity
        code order_uom
        decimal line_amount
    }
    SHIPMENT_LINE["Shipment Line · one item row dispatched"] {
        id shipment_line_id PK
        id shipment_id FK
        id sales_order_line_id FK
        id source_product_id FK
        id warehouse_id FK
        decimal shipped_quantity
    }
    BILLING_DOCUMENT["Billing Document · one invoice or credit memo"] {
        id billing_document_id PK
        id legal_entity_id FK
        id source_customer_id FK
        id sales_order_id FK
        id original_invoice_id FK
        decimal signed_total
    }
    BILLING_DOCUMENT_LINE["Billing Document Line · one billed item or charge"] {
        id billing_line_id PK
        id billing_document_id FK
        id sales_order_line_id FK
        id shipment_line_id FK
        id source_product_id FK
        id original_billing_line_id FK
        decimal signed_amount
    }
    SALES_ORDER o|..o{ BILLING_DOCUMENT : referenced_by
    BILLING_DOCUMENT ||..o{ BILLING_DOCUMENT_LINE : contains
    SALES_ORDER_LINE o|..o{ BILLING_DOCUMENT_LINE : billed_by
    SHIPMENT_LINE o|..o{ BILLING_DOCUMENT_LINE : billed_by
    BILLING_DOCUMENT o|..o{ BILLING_DOCUMENT : credited_by
    BILLING_DOCUMENT_LINE o|..o{ BILLING_DOCUMENT_LINE : credited_by
```

A header can have zero lines in Silver when the feed supplies document totals only, as with the dealer invoice CSV. The source order link and original invoice link are populated only where supported. ERPNext line references can trace an invoice through delivery back to the order. Credit quantity and postings do not establish a physical return.

## 5. Settlement and receivables

```mermaid
erDiagram
    direction TB
    SOURCE_CUSTOMER["Source Customer · one source customer account"] {
        id source_customer_id PK
        key native_customer_key
    }
    BILLING_DOCUMENT["Billing Document · one invoice or credit memo"] {
        id billing_document_id PK
        id legal_entity_id FK
        id source_customer_id FK
        id sales_order_id FK
        id original_invoice_id FK
        decimal signed_total
    }
    PAYMENT["Payment · one cash receipt or payment"] {
        id payment_id PK
        id legal_entity_id FK
        id source_customer_id FK
        id bank_account_id FK
        decimal received_amount
    }
    PAYMENT_ALLOCATION["Payment Allocation · one amount applied from a payment"] {
        id payment_allocation_id PK
        id payment_id FK
        id billing_document_id FK
        decimal allocated_amount
    }
    RECEIVABLES_SNAPSHOT["Receivables Snapshot · one invoice balance per cutoff and source"] {
        id billing_document_id PK, FK
        date as_of_date PK
        key balance_source PK
        decimal open_amount
    }
    SOURCE_CUSTOMER o|..o{ PAYMENT : pays
    PAYMENT ||..o{ PAYMENT_ALLOCATION : applies
    BILLING_DOCUMENT ||..o{ PAYMENT_ALLOCATION : receives
    BILLING_DOCUMENT ||..o{ RECEIVABLES_SNAPSHOT : measured_at
```

The payment-to-invoice many-to-many relationship is expressed through allocations. A payment can remain unallocated. Each AR balance keeps its own reporting cutoff and balance source. Preserve company and currency context when comparing source snapshots and calculated balances.

## 6. General ledger and voucher links

```mermaid
erDiagram
    direction TB
    LEGAL_ENTITY["Legal Entity · one operating company"] {
        id legal_entity_id PK
    }
    GL_ACCOUNT["Gl Account · one local chart of accounts entry"] {
        id gl_account_id PK
        id legal_entity_id FK
        id parent_account_id FK
        key native_account_key
    }
    GL_POSTING_LINE["Gl Posting Line · one debit or credit posting"] {
        id gl_posting_id PK
        id legal_entity_id FK
        id gl_account_id FK
        key voucher_type_and_native_id
        date posting_date
    }
    BILLING_DOCUMENT["Billing Document · one invoice or credit memo"] {
        id billing_document_id PK
        id legal_entity_id FK
        id source_customer_id FK
        id sales_order_id FK
        id original_invoice_id FK
        decimal signed_total
    }
    PAYMENT["Payment · one cash receipt or payment"] {
        id payment_id PK
        id legal_entity_id FK
        id source_customer_id FK
        id bank_account_id FK
        decimal received_amount
    }
    LEGAL_ENTITY ||..o{ GL_ACCOUNT : owns
    GL_ACCOUNT o|..o{ GL_ACCOUNT : parent_of
    GL_ACCOUNT ||..o{ GL_POSTING_LINE : receives
    BILLING_DOCUMENT o|..o{ GL_POSTING_LINE : originates
    PAYMENT o|..o{ GL_POSTING_LINE : originates
```

The billing and payment edges are conditional voucher references. Resolve using company, source, voucher type and ID. A posting may also come from another voucher type. The two displayed voucher links are mutually exclusive for a single posting. Account group nodes can sit in the hierarchy. Accepted postings reference posting accounts.

## 7. Storefront and point of sale

```mermaid
erDiagram
    direction TB
    SALES_ORDER["Sales Order · one order in a source company"] {
        id sales_order_id PK
        id legal_entity_id FK
        id source_customer_id FK
        date order_date
        code currency
        code order_status
    }
    ORDER_EVENT["Order Event · one logical storefront event"] {
        id order_event_id PK
        id legal_entity_id FK
        id sales_order_id FK
        key native_event_key
        timestamp occurred_at
    }
    POS_RECEIPT["Pos Receipt · one point of sale receipt"] {
        id pos_receipt_id PK
        id legal_entity_id FK
        id sales_order_id FK
        id cashier_source_user_id FK
        key native_receipt_key
    }
    POS_RECEIPT_LINE["Pos Receipt Line · one item on a receipt"] {
        id pos_receipt_line_id PK
        id pos_receipt_id FK
        id source_product_id FK
    }
    POS_TENDER["Pos Tender · one tender on a receipt"] {
        id pos_tender_id PK
        id pos_receipt_id FK
        code tender_code
        decimal tender_amount
    }
    SALES_ORDER o|..o{ ORDER_EVENT : referenced_by
    SALES_ORDER o|..o{ POS_RECEIPT : linked_with_evidence
    POS_RECEIPT ||..o{ POS_RECEIPT_LINE : contains
    POS_RECEIPT ||..o{ POS_TENDER : paid_with
```

Channel relationships require an explicit source reference or approved bridge. Kafka partition and offset identify deliveries of an event. The scoped event ID identifies the logical event. POS item lines reference source products. A tender amount remains empty if only the tender code was captured.

## 8. Bank reconciliation

```mermaid
erDiagram
    direction TB
    BANK_ACCOUNT["Bank Account · one bank account in a company"] {
        id bank_account_id PK
        id legal_entity_id FK
        key verified_account_key
    }
    BANK_STATEMENT_LINE["Bank Statement Line · one bank statement transaction"] {
        id bank_statement_line_id PK
        id bank_account_id FK
        key source_transaction_key
        date value_date
        decimal signed_amount
    }
    BANK_MATCH_ALLOCATION["Bank Match Allocation · one approved match amount"] {
        id bank_match_id PK
        id bank_statement_line_id FK
        id payment_id FK
        id gl_posting_id FK
        decimal matched_amount
    }
    PAYMENT["Payment · one cash receipt or payment"] {
        id payment_id PK
        id legal_entity_id FK
        id source_customer_id FK
        id bank_account_id FK
        decimal received_amount
    }
    GL_POSTING_LINE["Gl Posting Line · one debit or credit posting"] {
        id gl_posting_id PK
        id legal_entity_id FK
        id gl_account_id FK
        key voucher_type_and_native_id
        date posting_date
    }
    BANK_ACCOUNT ||..o{ BANK_STATEMENT_LINE : contains
    BANK_STATEMENT_LINE ||..o{ BANK_MATCH_ALLOCATION : reconciles
    PAYMENT o|..o{ BANK_MATCH_ALLOCATION : matched_as
    GL_POSTING_LINE o|..o{ BANK_MATCH_ALLOCATION : matched_as
    BANK_ACCOUNT o|..o{ PAYMENT : receives
```

Each match allocation selects one payment or one GL posting as its target. Related payment and GL evidence can support a single match without double counting. A statement line can have no confirmed match or several split matches.

## 9. Pricing and planning

```mermaid
erDiagram
    direction TB
    LEGAL_ENTITY["Legal Entity · one operating company"] {
        id legal_entity_id PK
    }
    PRICE_LIST["Price List · one pricing schedule"] {
        id price_list_id PK
        id legal_entity_id FK
    }
    PRICE_LIST_ITEM["Price List Item · one effective price per item unit and tier"] {
        id price_list_item_id PK
        id price_list_id FK
        id source_product_id FK
        code currency_and_uom
        date valid_from
    }
    SOURCE_PRODUCT["Source Product · one source sellable item"] {
        id source_product_id PK
        key native_item_key
    }
    BUDGET_AMOUNT["Budget Amount · one company period version and scope amount"] {
        id budget_amount_id PK
        id legal_entity_id FK
        key period_version_currency_scope
        decimal budget_amount
    }
    LEGAL_ENTITY ||..o{ PRICE_LIST : owns
    PRICE_LIST ||..o{ PRICE_LIST_ITEM : specifies
    SOURCE_PRODUCT ||..o{ PRICE_LIST_ITEM : priced_as
    LEGAL_ENTITY ||..o{ BUDGET_AMOUNT : plans
```

Price-list inputs are future coverage. Effective price scope includes unit, currency and quantity tier where applicable. The parent budget uses product category and the dealer budget uses local account. Each budget row carries its scope type and identifier. Validate the identifier against the applicable reference domain and compare actuals at that scope.

## 10. Shared reference values and actor identity

```mermaid
erDiagram
    direction TB
    REFERENCE_CODE["Reference Code · one code within a governed domain"] {
        code domain PK
        code value PK
    }
    SOURCE_USER["Source User · one actor account in a source"] {
        id source_user_id PK
        key native_user_key
    }
    SALES_ORDER["Sales Order · one order in a source company"] {
        id sales_order_id PK
        id legal_entity_id FK
        id source_customer_id FK
        date order_date
        code currency
        code order_status
    }
    SALES_ORDER_LINE["Sales Order Line · one item line on an order"] {
        id sales_order_line_id PK
        id sales_order_id FK
        id source_product_id FK
        id planned_warehouse_id FK
        decimal quantity
        code order_uom
        decimal line_amount
    }
    POS_RECEIPT["Pos Receipt · one point of sale receipt"] {
        id pos_receipt_id PK
        id legal_entity_id FK
        id sales_order_id FK
        id cashier_source_user_id FK
        key native_receipt_key
    }
    REFERENCE_CODE o|..o{ SALES_ORDER : supplies_currency
    REFERENCE_CODE o|..o{ SALES_ORDER : supplies_status
    REFERENCE_CODE o|..o{ SALES_ORDER_LINE : supplies_order_unit
    SOURCE_USER o|..o{ POS_RECEIPT : serves_as_cashier
```

Reference code roles use a scoped domain and value, such as currency, order status, unit, channel or tender. The displayed code joins are examples of reusable roles across Silver. User capture is outside the current Bronze contract. The cashier FK shows a future relationship where a source supplies an actor ID.

## 11. MDM customer crosswalk

```mermaid
erDiagram
    direction TB
    SOURCE_CUSTOMER["Source Customer · one source customer account"] {
        id source_customer_id PK
        key native_customer_key
    }
    CUSTOMER_CROSSWALK["Customer Crosswalk · one effective local to group customer mapping"] {
        id customer_mapping_id PK
        id source_customer_id FK
        id group_customer_id FK
        id match_candidate_id FK
        date valid_from
        date valid_to
    }
    GROUP_CUSTOMER["Group Customer · one approved group customer identity"] {
        id group_customer_id PK
    }
    SALES_ORDER["Sales Order · one order in a source company"] {
        id sales_order_id PK
        id legal_entity_id FK
        id source_customer_id FK
        date order_date
        code currency
        code order_status
    }
    MATCH_CANDIDATE["Match Candidate · one candidate decision case"] {
        id match_candidate_id PK
        key domain_and_candidate_keys
        code decision_status
        key evidence_reference
    }
    SOURCE_CUSTOMER o|..o{ SALES_ORDER : places
    SOURCE_CUSTOMER ||..o{ CUSTOMER_CROSSWALK : mapped_by
    GROUP_CUSTOMER ||..o{ CUSTOMER_CROSSWALK : groups
    MATCH_CANDIDATE o|..o{ CUSTOMER_CROSSWALK : supports
```

Multiple local customer accounts can map to one enterprise identity. Each local account can have at most one approved customer mapping effective at the lookup date. The order retains its local customer. An as-of join through the effective crosswalk supplies the group customer.

## 12. MDM product crosswalk and conversions

```mermaid
erDiagram
    direction TB
    SOURCE_PRODUCT["Source Product · one source sellable item"] {
        id source_product_id PK
        key native_item_key
    }
    PRODUCT_CROSSWALK["Product Crosswalk · one effective local to group product mapping"] {
        id product_mapping_id PK
        id source_product_id FK
        id group_product_id FK
        id match_candidate_id FK
        date valid_from
        date valid_to
    }
    GROUP_PRODUCT["Group Product · one approved group product identity"] {
        id group_product_id PK
    }
    UOM_CONVERSION["Uom Conversion · one approved conversion per scope and period"] {
        id conversion_id PK
        id group_product_id FK
        id source_product_id FK
        id group_customer_id FK
        code from_uom_and_to_uom
        decimal factor
        date valid_from
        date valid_to
    }
    MATCH_CANDIDATE["Match Candidate · one candidate decision case"] {
        id match_candidate_id PK
        key domain_and_candidate_keys
        code decision_status
        key evidence_reference
    }
    SOURCE_PRODUCT ||..o{ PRODUCT_CROSSWALK : mapped_by
    GROUP_PRODUCT ||..o{ PRODUCT_CROSSWALK : groups
    MATCH_CANDIDATE o|..o{ PRODUCT_CROSSWALK : supports
    GROUP_PRODUCT ||..o{ UOM_CONVERSION : converted_by
    SOURCE_PRODUCT o|..o{ UOM_CONVERSION : scopes
```

The order line joins to its source item as shown in view 1. Resolve group product via the crosswalk, then apply an approved conversion for that product and unit pair. A conversion may add source item, customer or agreement scope. One unambiguous approved rule must apply at the lookup date. Retain the recorded quantity, original unit and recorded amount alongside any conformed quantity.

## 13. MDM account classification

```mermaid
erDiagram
    direction TB
    GL_ACCOUNT["Gl Account · one local chart of accounts entry"] {
        id gl_account_id PK
        id legal_entity_id FK
        id parent_account_id FK
        key native_account_key
    }
    ACCOUNT_CROSSWALK["Account Crosswalk · one effective local account classification"] {
        id account_mapping_id PK
        id gl_account_id FK
        id reporting_account_category_id FK
        date valid_from
        date valid_to
    }
    REPORTING_ACCOUNT_CATEGORY["Reporting Account Category · one group reporting category"] {
        id reporting_account_category_id PK
        code category_code
    }
    GL_POSTING_LINE["Gl Posting Line · one debit or credit posting"] {
        id gl_posting_id PK
        id legal_entity_id FK
        id gl_account_id FK
        key voucher_type_and_native_id
        date posting_date
    }
    GL_ACCOUNT ||..o{ GL_POSTING_LINE : receives
    GL_ACCOUNT ||..o{ ACCOUNT_CROSSWALK : classified_by
    REPORTING_ACCOUNT_CATEGORY ||..o{ ACCOUNT_CROSSWALK : groups
```

Finance approves effective account classifications, such as dealer 400-D to Sales Revenue. Each account has at most one approved reporting category per applicable date in this model. Warehouse identities remain company-scoped. Verified bank account identity and bank matching remain separate relationships.

## 14. Lineage, replay and exceptions

```mermaid
erDiagram
    direction TB
    MODEL_RUN["Model Run · one Silver execution"] {
        id model_run_id PK
        key pinned_input_set
        key rule_and_mapping_versions
    }
    SOURCE_OBSERVATION["Source Observation · one modeled row to Bronze evidence link"] {
        id observation_id PK
        id model_run_id FK
        key modeled_entity_and_row_key
        key bronze_location_and_record_key
    }
    QUALITY_EXCEPTION["Quality Exception · one rule failure per input key per run"] {
        id quality_exception_id PK
        id model_run_id FK
        id observation_id FK
        code rule_id
        key input_record_key
    }
    MODEL_RUN ||..o{ SOURCE_OBSERVATION : records
    MODEL_RUN ||..o{ QUALITY_EXCEPTION : raises
    SOURCE_OBSERVATION o|..o{ QUALITY_EXCEPTION : evidences
```

The lineage target is a typed reference to any modeled entity and row version. Multiple observations can support one business record. Record source manifest or Kafka delivery coordinates, business-effective time where available, observation time, and the rule and mapping versions used. An identical replay can add observation evidence without creating a new business version.

## Relationships that connect the views

| Foreign key holder | Referenced entity | Cardinality and rule |
|---|---|---|
| Shipment, Return Document, Billing Document, Payment, GL Posting Line, Inventory Movement Line, Bank Account, Order Event and POS Receipt | Legal Entity | Each record belongs to one company. Each company can own many records. |
| Billing Document | Source Customer | Each document has zero or one resolved source customer. A customer can have many documents. Keep the unresolved native reference when evidence is incomplete. |
| Shipment Line, Return Line, Inventory Movement Line and POS Receipt Line | Source Product | Each accepted merchandise or movement line identifies one source product. That product can appear on many lines. |
| Billing Document Line | Source Product | Zero or one product per line. A charge-only line may omit the product. |
| UOM Conversion | Group Customer | Zero or one scoped group customer per conversion. A customer may have many applicable rules across products, units and periods. |

## How MDM decisions become Silver joins

The ERDs above show the persistent relationships. This flow shows how decisions reach those tables while local transactions remain available for reconciliation.

```mermaid
flowchart TD
    S["Profile and standardize local masters"] --> K["Generate candidate and record evidence"]
    K --> R{"Unique verified match?"}
    R -->|Yes| A["Approve by rule"]
    R -->|Review needed| T["Steward review"]
    T -->|More evidence needed| P["Pending candidate"]
    P -->|New evidence| T
    T -->|Confirmed| A
    T -->|Confirmed distinct| I["Approve distinct identities"]
    T -->|Rejected| D["Record rejection"]
    I --> X
    A --> X["Publish effective crosswalk"]
    X --> J["Join using source key and business date"]
```

| Fixture | Crosswalk or rule | Expected result |
|---|---|---|
| Metro customer | Verified registration `DEMO-100`, with the parent fixture alias linked to its native Customer ID | Parent and dealer accounts roll up to one group customer. Apex stays local while evidence is pending. |
| Pump | Verified `ACME / RP100` | P-PUMP and D-PUMP roll up to one group product. The Metro slice still reconciles to two orders, 11 pumps and ₹140,000 in order value. |
| Seal pack | Product identity approval plus a separate approved conversion of one dealer pack to ten each | Source quantity stays one pack. Ten each is available for group quantity reporting once the conversion is effective. |
| Mapping correction | Publish a superseding decision with reason, evidence and effective period | Reproduce older outputs with the recorded version or restate approved outputs with the new version. Review validity overlaps before publishing. |

When reviewing a join, trace the scoped source key, verify the allowed cardinality, and compare record counts and amounts before and after the join. Check unresolved customer links, repeated product lines, split shipments, document-only dealer billing, replayed channel events, and overlapping mapping dates.
