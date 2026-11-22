CREATE VIEW sem_sales_daily AS
SELECT day, legal_entity_id, currency, booked_amount, net_billed_amount,
       distinct_orders AS booked_orders, gross_margin, coverage
FROM gold.gold_sales_daily;

CREATE VIEW sem_order_lines AS
WITH fulfillment AS (
    SELECT sales_order_line_id, count(*) AS shipment_lines,
           count(DISTINCT shipment_id) AS linked_shipments
    FROM gold.fact_shipment_line
    WHERE sales_order_line_id IS NOT NULL
    GROUP BY sales_order_line_id
)
SELECT b.booking_date, b.legal_entity_id, b.source_system, b.sales_order_id,
       b.order_reference, b.order_line_id, b.currency, b.booked_amount,
       b.source_qty, b.source_uom, b.booked_qty, b.booked_uom,
       coalesce(nullif(c.business_id, '__unknown__'), 'Unresolved') AS customer,
       coalesce(nullif(p.business_id, '__unknown__'), 'Unresolved') AS product,
       coalesce(f.shipment_lines, 0) AS linked_shipment_lines,
       coalesce(f.linked_shipments, 0) AS linked_shipments
FROM gold.fact_sales_order_booking b
LEFT JOIN fulfillment f ON b.id = f.sales_order_line_id
LEFT JOIN gold.dim_customer c ON b.customer_key = c.key
LEFT JOIN gold.dim_product p ON b.product_key = p.key
WHERE b.booking_date <= (SELECT cutoff FROM semantic_release);

CREATE VIEW sem_customer_360 AS
SELECT coalesce(nullif(c.business_id, '__unknown__'), 'Unresolved') AS customer,
       m.currency, m.as_of_date, m.observed_net_billed_amount, m.lifetime_net_billed_amount,
       m.last_observed_order_date, m.observed_distinct_orders, m.inactive_120_days, m.coverage
FROM gold.gold_customer_360 m
LEFT JOIN gold.dim_customer c ON m.customer_key = c.key;

CREATE VIEW sem_orders_5m AS
SELECT window_start_utc, coalesce(nullif(channel, '__unknown__'), 'Unknown') AS channel,
       accepted_distinct_orders, cancellation_events
FROM gold.gold_orders_live;

CREATE VIEW sem_ar_aging AS
SELECT a.as_of_date, a.legal_entity_id, a.invoice_id,
       coalesce(nullif(c.business_id, '__unknown__'), 'Unresolved') AS customer,
       a.currency, a.invoice_date, a.due_date, a.days_past_due, a.aging_bucket,
       a.open_amount, a.balance_basis, a.coverage_status
FROM gold.gold_ar_aging a
LEFT JOIN gold.dim_customer c ON a.customer_key = c.key;

CREATE VIEW sem_collections_monthly AS
SELECT due_month, legal_entity_id, currency, as_of_date, invoices, gross_due_amount,
       eligible_due_amount, cash_by_payment_posting_date, collection_rate, avg_days_to_pay, coverage
FROM gold.gold_collections_monthly;

CREATE VIEW sem_pnl_vs_budget AS
SELECT period, fiscal_period, legal_entity_id, scope_type, scope, scenario, budget_version,
       currency, budget_amount, actual_amount, variance, coverage, as_of_date
FROM gold.gold_pnl_vs_budget;

CREATE VIEW sem_revenue_reconciliation AS
SELECT legal_entity_id, billing_document_id, currency, as_of_date, signed_billed_amount,
       posted_revenue, difference, status, amount_basis
FROM gold.gold_revenue_reconciliation;

CREATE VIEW sem_revenue_unlinked_postings AS
SELECT legal_entity_id, posting_date, voucher_id, gl_entry_id, account_source_id,
       reporting_category, currency, debit_amount, credit_amount
FROM gold.gold_revenue_unlinked_postings;

-- Filter first and then count distinct scoped orders. The currency is mandatory
-- so order values are never aggregated across currencies.
CREATE MACRO sales_kpis(from_day, to_day, entity, selected_currency) AS TABLE
WITH order_totals AS (
    SELECT count(DISTINCT sales_order_id) AS booked_orders,
           count(DISTINCT sales_order_id) FILTER (WHERE linked_shipment_lines > 0) AS orders_with_linked_shipment
    FROM sem_order_lines
    WHERE booking_date BETWEEN from_day AND to_day
      AND (entity IS NULL OR legal_entity_id = entity) AND currency = selected_currency
), amounts AS (
    SELECT count(*) AS daily_rows, sum(booked_amount) AS booked_amount, sum(net_billed_amount) AS net_billed_amount
    FROM sem_sales_daily
    WHERE day BETWEEN from_day AND to_day
      AND (entity IS NULL OR legal_entity_id = entity) AND currency = selected_currency
)
SELECT booked_orders, orders_with_linked_shipment, daily_rows, booked_amount, net_billed_amount
FROM order_totals CROSS JOIN amounts;
